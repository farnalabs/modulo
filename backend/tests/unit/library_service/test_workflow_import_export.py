"""Unit tests for workflow bundle export/import helpers."""

import io
import json
import uuid
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.workflow_import_export import (
    BUNDLE_FORMAT_VERSION,
    MANIFEST_FILENAME,
    extract_bundle_json_from_zip,
    suggest_import_name,
)


def _empty_set() -> set:
    return set()


class TestSuggestImportName:
    def test_no_conflict_returns_original(self):
        existing = {"foo", "bar"}
        assert suggest_import_name(existing, "baz") == "baz"

    def test_conflict_appends_suffix(self):
        existing = {"My Agent"}
        assert suggest_import_name(existing, "My Agent") == "My Agent (imported)"

    def test_conflict_with_existing_suffixed_appends_counter(self):
        existing = {"My Agent", "My Agent (imported)", "My Agent (imported) 2"}
        assert suggest_import_name(existing, "My Agent") == "My Agent (imported) 3"

    def test_empty_existing_set(self):
        assert suggest_import_name(set(), "anything") == "anything"

    def test_custom_suffix(self):
        existing = {"task"}
        result = suggest_import_name(existing, "task", suffix="(copy)")
        assert result == "task (copy)"

    def test_case_sensitive(self):
        existing = {"Agent"}
        assert suggest_import_name(existing, "agent") == "agent"
        assert suggest_import_name(existing, "Agent") == "Agent (imported)"

    @pytest.mark.parametrize("include_max_existing", [True, False], ids=["max-existing", "max-free"])
    def test_counter_never_overflows_name_column(self, include_max_existing):
        name = "A" * 255
        base = name[: 255 - len("(imported)") - 6]
        existing = {name, f"{base} (imported)"}
        existing.update(f"{base} (imported) {idx}" for idx in range(2, 10000 if include_max_existing else 9999))
        result = suggest_import_name(existing, name)
        assert len(result) <= 255
        # The reserved-count digit cap must clamp the counter to 9999 whether or
        # not the max counter already exists.
        assert result == f"{base} (imported) 9999"


class TestExtractBundleJson:
    def _make_zip(self, bundle: dict) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(MANIFEST_FILENAME, json.dumps(bundle))
        return buf.getvalue()

    def test_extracts_bundle_json_from_valid_zip(self):
        bundle = {"format_version": BUNDLE_FORMAT_VERSION, "pipeline": {"name": "Test"}}
        zip_bytes = self._make_zip(bundle)
        result = extract_bundle_json_from_zip(zip_bytes)
        assert result["format_version"] == BUNDLE_FORMAT_VERSION
        assert result["pipeline"]["name"] == "Test"

    def test_raises_on_missing_manifest(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("other.json", "{}")
        with pytest.raises(LookupError, match=MANIFEST_FILENAME):
            extract_bundle_json_from_zip(buf.getvalue())

    def test_raises_on_invalid_json(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(MANIFEST_FILENAME, "not json")
        with pytest.raises(ValueError, match="malformed JSON"):
            extract_bundle_json_from_zip(buf.getvalue())


class TestMaterializeImport:
    """Integration-style tests using mocked CRUD layer."""

    @pytest.fixture
    def mock_session(self):
        session = MagicMock()
        session.flush = AsyncMock()
        session.execute = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=ctx)
        ctx.__aexit__ = AsyncMock(return_value=False)
        session.begin = MagicMock(return_value=ctx)
        session.in_transaction = MagicMock(return_value=True)
        scalar_result = MagicMock()
        scalar_result.scalar_one_or_none.return_value = None
        scalar_result.scalars.return_value = []
        session.execute.return_value = scalar_result
        return session

    @pytest.fixture
    def minimal_bundle(self):
        return {
            "format_version": BUNDLE_FORMAT_VERSION,
            "pipeline": {
                "name": "Imported Pipeline",
                "description": "A test import",
                "graph_nodes_json": [],
                "run_context_defaults": {},
                "node_timeout_seconds": 300,
            },
            "agents": [],
            "schemas": [],
            "model_backends": [],
            "edges": [],
        }

    async def test_materialize_minimal_bundle(self, mock_session, minimal_bundle):
        # Minimal bundle with no agents, schemas, or edges creates just pipeline + library primitive
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()

        with (
            patch(
                "modulo.core.workflow_import_export.get_existing_agent_names",
                AsyncMock(return_value=_empty_set()),
            ),
            patch(
                "modulo.core.workflow_import_export.get_existing_pipeline_names",
                AsyncMock(return_value=_empty_set()),
            ),
            patch("modulo.core.workflow_import_export.create_agent", AsyncMock()),
            patch("modulo.core.workflow_import_export.create_pipeline") as mock_cp,
            patch("modulo.core.workflow_import_export.create_schema", AsyncMock()),
            patch("modulo.core.workflow_import_export.create_schema_version", AsyncMock()),
            patch("modulo.core.workflow_import_export.create_library_primitive", AsyncMock()),
        ):
            mock_cp.return_value.id = uuid.uuid4()
            mock_cp.return_value.name = "Imported Pipeline"

            from modulo.core.workflow_import_export import materialize_import

            result = await materialize_import(
                mock_session,
                org_id=org_id,
                created_by=user_id,
                bundle=minimal_bundle,
            )

        assert result["pipeline_name"] == "Imported Pipeline"
        assert result["agent_count"] == 0
        assert result["edge_count"] == 0
        assert result["schema_count"] == 0
        mock_cp.assert_awaited_once()

    async def test_materialize_with_agents(self, mock_session):
        bundle = {
            "format_version": BUNDLE_FORMAT_VERSION,
            "pipeline": {"name": "Agent Pipeline", "graph_nodes_json": []},
            "agents": [
                {
                    "id": str(uuid.uuid4()),
                    "name": "My Agent",
                    "input_schema_id": str(uuid.uuid4()),
                    "input_schema_version": "1.0",
                    "output_schema_id": str(uuid.uuid4()),
                    "output_schema_version": "1.0",
                    "prompt_template": "Do something",
                    "model_backend_id": str(uuid.uuid4()),
                }
            ],
            "schemas": [],
            "model_backends": [],
            "edges": [],
        }
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()

        with (
            patch(
                "modulo.core.workflow_import_export.get_existing_agent_names",
                AsyncMock(return_value=_empty_set()),
            ),
            patch(
                "modulo.core.workflow_import_export.get_existing_pipeline_names",
                AsyncMock(return_value=_empty_set()),
            ),
            patch("modulo.core.workflow_import_export.create_agent") as mock_ca,
            patch("modulo.core.workflow_import_export.create_pipeline") as mock_cp,
            patch("modulo.core.workflow_import_export.create_schema", AsyncMock()),
            patch("modulo.core.workflow_import_export.create_schema_version", AsyncMock()),
            patch("modulo.core.workflow_import_export.create_library_primitive", AsyncMock()),
        ):
            mock_ca.return_value.id = uuid.uuid4()
            mock_cp.return_value.id = uuid.uuid4()

            from modulo.core.workflow_import_export import materialize_import

            result = await materialize_import(
                mock_session,
                org_id=org_id,
                created_by=user_id,
                bundle=bundle,
            )

        assert result["agent_count"] == 1
        mock_ca.assert_awaited_once()

    async def test_materialize_with_name_conflict(self, mock_session):
        bundle = {
            "format_version": BUNDLE_FORMAT_VERSION,
            "pipeline": {"name": "Existing Pipeline", "graph_nodes_json": []},
            "agents": [],
            "schemas": [],
            "model_backends": [],
            "edges": [],
        }
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()

        with (
            patch(
                "modulo.core.workflow_import_export.get_existing_agent_names",
                AsyncMock(return_value=_empty_set()),
            ),
            patch(
                "modulo.core.workflow_import_export.get_existing_pipeline_names",
                AsyncMock(return_value={"Existing Pipeline"}),
            ),
            patch("modulo.core.workflow_import_export.create_pipeline") as mock_cp,
            patch("modulo.core.workflow_import_export.create_library_primitive", AsyncMock()),
        ):
            mock_cp.return_value.id = uuid.uuid4()
            mock_cp.return_value.name = ""

            from modulo.core.workflow_import_export import materialize_import

            result = await materialize_import(
                mock_session,
                org_id=org_id,
                created_by=user_id,
                bundle=bundle,
            )

        assert result["pipeline_name"] == "Existing Pipeline (imported)"
        captured_name = mock_cp.call_args.kwargs["name"]
        assert captured_name == "Existing Pipeline (imported)"

    async def test_materialize_creates_missing_schemas(self, mock_session):
        schema_id = str(uuid.uuid4())
        bundle = {
            "format_version": BUNDLE_FORMAT_VERSION,
            "pipeline": {"name": "Schema Pipeline", "graph_nodes_json": []},
            "agents": [],
            "schemas": [
                {
                    "id": schema_id,
                    "name": "New Schema",
                    "definition_json": {"type": "object", "properties": {}},
                    "latest_version": "1.0",
                }
            ],
            "model_backends": [],
            "edges": [],
        }
        org_id = uuid.uuid4()
        user_id = uuid.uuid4()

        with (
            patch(
                "modulo.core.workflow_import_export.get_existing_agent_names",
                AsyncMock(return_value=_empty_set()),
            ),
            patch(
                "modulo.core.workflow_import_export.get_existing_pipeline_names",
                AsyncMock(return_value=_empty_set()),
            ),
            patch("modulo.core.workflow_import_export.create_agent", AsyncMock()),
            patch("modulo.core.workflow_import_export.create_pipeline") as mock_cp,
            patch("modulo.core.workflow_import_export.create_schema") as mock_cs,
            patch("modulo.core.workflow_import_export.create_schema_version") as mock_csv,
            patch("modulo.core.workflow_import_export.create_library_primitive", AsyncMock()),
        ):
            mock_cp.return_value.id = uuid.uuid4()
            mock_cs.return_value.id = uuid.uuid4()

            from modulo.core.workflow_import_export import materialize_import

            result = await materialize_import(
                mock_session,
                org_id=org_id,
                created_by=user_id,
                bundle=bundle,
            )

        assert result["schema_count"] == 1
        mock_cs.assert_awaited_once()
        mock_csv.assert_awaited_once()
