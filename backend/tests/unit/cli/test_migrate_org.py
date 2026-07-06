"""Tests for the modulo (argparse) migration CLI tool."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from modulo.cli.migrate_org import (
    ENTITY_ORDER,
    FK_COLUMNS,
    _compute_hash,
    _remap_fk,
    _serialise,
    _serialise_row,
    _verify_hash,
    build_parser,
)
from tests.unit.cli.conftest import MockModel


class TestSerialise:
    def test_handles_uuids(self) -> None:
        uid = uuid.uuid4()
        assert _serialise(uid) == str(uid)

    def test_handles_datetime(self) -> None:
        dt = datetime.now(UTC)
        result = _serialise(dt)
        assert result == dt.isoformat()

    def test_handles_bytes(self) -> None:
        assert _serialise(b"\x00\xff") == "00ff"

    def test_handles_decimal(self) -> None:
        assert _serialise(Decimal("3.14")) == 3.14

    def test_handles_set(self) -> None:
        assert _serialise({1, 2, 3}) == [1, 2, 3]

    def test_handles_none(self) -> None:
        assert _serialise(None) is None

    def test_handles_int(self) -> None:
        assert _serialise(42) == 42

    def test_handles_string(self) -> None:
        assert _serialise("hello") == "hello"


class TestSerialiseRow:
    def test_serialises_all_columns(self) -> None:
        uid = uuid.uuid4()
        row = MockModel(id=uid, name="hello", count=42)
        result = _serialise_row(row)
        assert result["id"] == str(uid)
        assert result["name"] == "hello"
        assert result["count"] == 42

    def test_skips_none_values(self) -> None:
        row = MockModel(id=uuid.uuid4(), name=None)
        result = _serialise_row(row)
        assert "name" not in result

    def test_handles_datetime_column(self) -> None:
        dt = datetime.now(UTC)
        row = MockModel(ts=dt)
        result = _serialise_row(row)
        assert result["ts"] == dt.isoformat()

    def test_handles_bytes_column(self) -> None:
        row = MockModel(blob=b"\xde\xad")
        result = _serialise_row(row)
        assert result["blob"] == "dead"


class TestComputeHash:
    def test_empty_bundle(self) -> None:
        bundle: dict = {}
        h = _compute_hash(bundle)
        assert isinstance(h, str)
        assert len(h) == 64

    def test_deterministic(self) -> None:
        bundle = {"users": [{"id": "u1", "name": "alice"}]}
        h1 = _compute_hash(bundle)
        h2 = _compute_hash(bundle)
        assert h1 == h2

    def test_with_meta_and_data(self) -> None:
        bundle = {
            "__meta__": {"version": 1, "exported_at": "2024-01-01T00:00:00"},
            "users": [{"id": "u1", "name": "alice"}],
        }
        h = _compute_hash(bundle)
        assert isinstance(h, str)
        assert len(h) == 64

    def test_excludes_hash_from_computation(self) -> None:
        bundle = {
            "__meta__": {"version": 1, "hash": "will-be-excluded"},
            "users": [{"id": "u1"}],
        }
        h = _compute_hash(bundle)
        assert len(h) == 64
        assert "will-be-excluded" not in h


class TestVerifyHash:
    def test_valid_hash_returns_true(self) -> None:
        bundle = {
            "__meta__": {"version": 1},
            "users": [{"id": "u1"}],
        }
        bundle["__meta__"]["hash"] = _compute_hash(bundle)
        assert _verify_hash(bundle) is True

    def test_invalid_hash_returns_false(self) -> None:
        bundle = {
            "__meta__": {"version": 1, "hash": "aaaa"},
            "users": [{"id": "u1"}],
        }
        assert _verify_hash(bundle) is False

    def test_missing_hash_returns_false(self) -> None:
        bundle = {
            "__meta__": {"version": 1},
            "users": [{"id": "u1"}],
        }
        assert _verify_hash(bundle) is False


class TestEntityOrder:
    def test_includes_all_expected_entities(self) -> None:
        names = [name for name, _ in ENTITY_ORDER]
        assert "users" in names
        assert "teams" in names
        assert "stages" in names
        assert "schemas" in names
        assert "schema_versions" in names
        assert "model_backends" in names
        assert "library_primitives" in names
        assert "connector_instances" in names
        assert "agents" in names
        assert "pipelines" in names
        assert "runs" in names
        assert len(names) == 11

    def test_users_before_pipelines(self) -> None:
        names = [name for name, _ in ENTITY_ORDER]
        assert names.index("users") < names.index("pipelines")

    def test_has_correct_types(self) -> None:
        from modulo.db.models import Account, Pipeline

        model_types = {name: cls for name, cls in ENTITY_ORDER}
        assert model_types["users"] is Account
        assert model_types["pipelines"] is Pipeline
        assert "organisation" not in model_types


class TestFkColumns:
    def test_users_has_org_fk(self) -> None:
        assert "organisation_id" in FK_COLUMNS["users"]

    def test_agents_has_model_backend_fk(self) -> None:
        assert "model_backend_id" in FK_COLUMNS["agents"]

    def test_runs_has_pipeline_fk(self) -> None:
        assert "pipeline_id" in FK_COLUMNS["runs"]

    def test_all_entity_types_have_fk_entry(self) -> None:
        entity_names = {name for name, _ in ENTITY_ORDER}
        fk_names = set(FK_COLUMNS.keys())
        assert entity_names == fk_names


class TestRemapFk:
    def test_remaps_organisation_id(self) -> None:
        old_org = str(uuid.uuid4())
        new_org = str(uuid.uuid4())
        row = {"organisation_id": old_org, "name": "test"}
        id_map = {old_org: new_org}
        result = _remap_fk(row, "users", id_map)
        assert str(result["organisation_id"]) == new_org

    def test_remaps_multiple_fks(self) -> None:
        old_org = str(uuid.uuid4())
        old_team = str(uuid.uuid4())
        new_org = str(uuid.uuid4())
        new_team = str(uuid.uuid4())
        row = {"organisation_id": old_org, "owner_team_id": old_team, "name": "test"}
        id_map = {old_org: new_org, old_team: new_team}
        result = _remap_fk(row, "stages", id_map)
        assert str(result["organisation_id"]) == new_org
        assert str(result["owner_team_id"]) == new_team

    def test_skips_unknown_fks(self) -> None:
        row = {"organisation_id": "unknown-uuid", "name": "test"}
        result = _remap_fk(row, "users", {})
        assert result["organisation_id"] == "unknown-uuid"

    def test_returns_new_dict(self) -> None:
        row = {"name": "test"}
        result = _remap_fk(row, "users", {})
        assert result is not row
        assert result["name"] == "test"


class TestBuildParser:
    def test_creates_export_org_subcommand(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["export-org", "--org-id", str(uuid.uuid4())])
        assert args.command == "export-org"

    def test_creates_import_org_subcommand(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["import-org", "--org-id", str(uuid.uuid4()), "--input", "test.json"])
        assert args.command == "import-org"

    def test_export_org_requires_org_id(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["export-org"])

    def test_import_org_requires_input(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["import-org", "--org-id", str(uuid.uuid4())])

    def test_export_org_default_output(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["export-org", "--org-id", str(uuid.uuid4())])
        assert args.output == Path("export.json")

    def test_export_org_custom_output(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["export-org", "--org-id", str(uuid.uuid4()), "--output", "custom.json"])
        assert args.output == Path("custom.json")

    def test_import_org_default_conflict(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["import-org", "--org-id", str(uuid.uuid4()), "--input", "data.json"])
        assert args.conflict == "skip"

    def test_import_org_custom_conflict(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "import-org",
                "--org-id",
                str(uuid.uuid4()),
                "--input",
                "data.json",
                "--conflict",
                "rename",
            ]
        )
        assert args.conflict == "rename"

    def test_export_org_sets_func(self) -> None:
        from modulo.cli.migrate_org import cmd_export

        parser = build_parser()
        args = parser.parse_args(["export-org", "--org-id", str(uuid.uuid4())])
        assert args.func is cmd_export

    def test_import_org_sets_func(self) -> None:
        from modulo.cli.migrate_org import cmd_import

        parser = build_parser()
        args = parser.parse_args(["import-org", "--org-id", str(uuid.uuid4()), "--input", "data.json"])
        assert args.func is cmd_import
