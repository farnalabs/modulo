"""Unit tests for workflow bundle resilience fixes (cross-cutting QA)."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import status
from sqlalchemy.exc import SQLAlchemyError

from modulo.core.workflow_import_export import BUNDLE_FORMAT_VERSION, materialize_import


class TestConfirmImportParameterFix:
    """Verify materialize_import receives created_by, not account_id."""

    async def test_materialize_called_with_created_by(self):
        """confirm_import_endpoint should pass created_by=, not account_id=."""
        from modulo.api.routes.library import confirm_import_endpoint

        mock_session = MagicMock()
        mock_session.begin = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin.return_value = ctx
        mock_session.execute = AsyncMock()

        mock_principal = MagicMock()
        mock_principal.organisation_id = uuid.uuid4()
        mock_principal.account_id = uuid.uuid4()
        mock_principal.org_role = "admin"

        mock_body = MagicMock()
        mock_body.bundle_json = json.dumps({
            "format_version": BUNDLE_FORMAT_VERSION,
            "pipeline": {"name": "Test", "graph_nodes_json": []},
            "agents": [],
            "schemas": [],
            "model_backends": [],
            "edges": [],
        })
        mock_body.owner_team_id = None
        mock_body.pipeline_name_override = None
        mock_body.model_backend_overrides = None
        mock_body.schema_overrides = None
        mock_body.schema_version_overrides = None
        mock_body.connector_overrides = None

        with (
            patch("modulo.api.routes.library.materialize_import", AsyncMock()) as mock_mi,
            patch("modulo.api.routes.library.set_rls_org", AsyncMock()),
            patch("modulo.api.routes.library.set_rls_user_context", AsyncMock()),
        ):
            mock_mi.return_value = {
                "pipeline_id": str(uuid.uuid4()),
                "pipeline_name": "Test",
                "primitive_id": str(uuid.uuid4()),
                "agent_count": 0,
                "edge_count": 0,
                "schema_count": 0,
                "warnings": [],
            }

            await confirm_import_endpoint(mock_body, mock_session, mock_principal)

        # Verify materialize_import was called with 'created_by', not 'account_id'
        call_kwargs = mock_mi.call_args.kwargs
        assert "created_by" in call_kwargs, "materialize_import must be called with created_by="
        assert "account_id" not in call_kwargs, "materialize_import must NOT be called with account_id="
        assert call_kwargs["created_by"] == mock_principal.account_id


class TestAnalyseBundleSQLAlchemyError:
    """_analyse_bundle should return 503 on SQLAlchemyError."""

    async def test_sqlalchemy_error_returns_503(self):
        from modulo.api.routes.library import _analyse_bundle

        mock_session = MagicMock()
        mock_session.begin = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(side_effect=SQLAlchemyError("mock db failure"))
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin.return_value = ctx

        mock_principal = MagicMock()
        mock_principal.organisation_id = uuid.uuid4()
        mock_principal.account_id = uuid.uuid4()
        mock_principal.org_role = "admin"

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await _analyse_bundle(mock_session, mock_principal, {"pipeline": {"name": "Test"}, "schemas": [], "agents": [], "model_backends": []})

        assert exc_info.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


class TestConfirmImportSQLAlchemyError:
    """confirm_import_endpoint should return 503 on SQLAlchemyError."""

    async def test_sqlalchemy_error_returns_503(self):
        from modulo.api.routes.library import confirm_import_endpoint

        mock_session = MagicMock()
        mock_session.begin = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(side_effect=SQLAlchemyError("mock db failure"))
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin.return_value = ctx

        mock_principal = MagicMock()
        mock_principal.organisation_id = uuid.uuid4()
        mock_principal.account_id = uuid.uuid4()
        mock_principal.org_role = "admin"

        mock_body = MagicMock()
        mock_body.bundle_json = json.dumps({
            "format_version": BUNDLE_FORMAT_VERSION,
            "pipeline": {"name": "Test", "graph_nodes_json": []},
            "agents": [],
            "schemas": [],
            "model_backends": [],
            "edges": [],
        })
        mock_body.owner_team_id = None
        mock_body.pipeline_name_override = None
        mock_body.model_backend_overrides = None
        mock_body.schema_overrides = None
        mock_body.schema_version_overrides = None
        mock_body.connector_overrides = None

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await confirm_import_endpoint(mock_body, mock_session, mock_principal)

        assert exc_info.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


class TestExportPipelineSingleTransaction:
    """export_pipeline_endpoint should use a single session.begin()."""

    async def test_single_transaction(self):
        from modulo.api.routes.library import export_pipeline_endpoint

        pipeline_id = uuid.uuid4()
        mock_session = MagicMock()
        mock_session.begin = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.begin.return_value = ctx

        mock_pipeline = MagicMock()
        mock_pipeline.name = "Test Pipeline"
        mock_pipeline.id = pipeline_id

        mock_principal = MagicMock()
        mock_principal.organisation_id = uuid.uuid4()
        mock_principal.account_id = uuid.uuid4()
        mock_principal.org_role = "admin"

        with (
            patch("modulo.api.routes.library.get_pipeline", AsyncMock(return_value=mock_pipeline)),
            patch("modulo.api.routes.library.export_pipeline_bundle", AsyncMock(return_value=b"zip-content")),
            patch("modulo.api.routes.library.set_rls_org", AsyncMock()),
            patch("modulo.api.routes.library.set_rls_user_context", AsyncMock()),
        ):
            response = await export_pipeline_endpoint(pipeline_id, mock_session, mock_principal)

        # session.begin should have been called exactly once
        assert mock_session.begin.call_count == 1, "export_pipeline_endpoint should use a single session.begin()"
        assert response.status_code == 200


class TestMaterializeInvalidOwnerTeamId:
    """materialize_import should raise ValueError for non-existent owner_team_id."""

    async def test_nonexistent_owner_team_id_raises_value_error(self):
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()
        fake_team_id = uuid.uuid4()

        mock_session = MagicMock()
        scalar_result = MagicMock()
        scalar_result.scalar_one_or_none.return_value = None  # team not found
        mock_session.execute = AsyncMock(return_value=scalar_result)

        bundle = {
            "format_version": BUNDLE_FORMAT_VERSION,
            "pipeline": {"name": "Test", "graph_nodes_json": []},
            "agents": [],
            "schemas": [],
            "model_backends": [],
            "edges": [],
        }


        with pytest.raises(ValueError, match=f"Team {fake_team_id} not found in this organisation"):
            await materialize_import(
                mock_session,
                org_id=org_id,
                created_by=user_id,
                bundle=bundle,
                owner_team_id=fake_team_id,
            )

        # Verify the query was for the right team + org
        call_args = mock_session.execute.call_args
        if call_args:
            stmt = call_args[0][0]
            compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            # UUIDs in compiled SQL may be hex without dashes
            assert str(fake_team_id).replace("-", "") in compiled.replace("-", "")
