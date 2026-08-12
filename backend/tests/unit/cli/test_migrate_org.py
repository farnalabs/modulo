"""Tests for the modulo export-org / import-org CLI (argparse-based)."""

import json
import uuid
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.cli.migrate_org import (
    _compute_hash,
    _load_bundle,
    _parse_uuid,
    _remap_fk,
    _serialise,
    _serialise_row,
    _verify_hash,
    _write_bundle,
    main,
)
from tests.unit.cli.conftest import MockModel

# ── Pure function tests ─────────────────────────────────────────────────────


class TestParseUuid:
    def test_valid(self) -> None:
        uid = uuid.uuid4()
        result = _parse_uuid(str(uid), "test")
        assert result == uid

    def test_invalid_exits(self) -> None:
        with pytest.raises(SystemExit):
            _parse_uuid("not-a-uuid", "test")

    def test_non_string_attribute_error_exits(self) -> None:
        # uuid.UUID() on a non-string raises AttributeError, which must also be
        # converted into a SystemExit rather than escaping as a raw traceback.
        with pytest.raises(SystemExit) as exc:
            _parse_uuid(12345, "test")
        assert "Invalid" in str(exc.value)


class TestSerialise:
    def test_serialise_uuid(self) -> None:
        uid = uuid.uuid4()
        assert _serialise(uid) == str(uid)

    def test_serialise_datetime(self) -> None:
        from datetime import UTC, datetime

        dt = datetime.now(UTC)
        assert _serialise(dt) == dt.isoformat()

    def test_serialise_bytes(self) -> None:
        assert _serialise(b"\x00\xff") == "00ff"

    def test_serialise_decimal(self) -> None:
        from decimal import Decimal

        assert _serialise(Decimal("10.5")) == "10.5"

    def test_serialise_set(self) -> None:
        # Set ordering is not guaranteed by _serialise; compare as sets.
        assert set(_serialise({1, 2, 3})) == {1, 2, 3}

    def test_serialise_none(self) -> None:
        assert _serialise(None) is None

    def test_serialise_passthrough_unknown(self) -> None:
        assert _serialise("plain") == "plain"
        assert _serialise(42) == 42
        assert _serialise([1, "two"]) == [1, "two"]
        assert _serialise({"nested": {"k": 1}}) == {"nested": {"k": 1}}


class TestSerialiseRow:
    def test_handles_all_types(self) -> None:
        from datetime import UTC, datetime

        uid = uuid.uuid4()
        dt = datetime.now(UTC)
        row = MockModel(id=uid, name="hello", ts=dt, blob=b"\x01", null_col=None)
        result = _serialise_row(row)
        assert result["id"] == str(uid)
        assert result["name"] == "hello"
        assert result["ts"] == dt.isoformat()
        assert result["blob"] == "01"
        assert result["null_col"] is None

    def test_handles_decimal_and_set(self) -> None:
        from decimal import Decimal

        row = MockModel(id=uuid.uuid4(), price=Decimal("19.99"), tags={"a", "b"})
        result = _serialise_row(row)
        assert result["price"] == "19.99"
        assert set(result["tags"]) == {"a", "b"}


class TestHash:
    def test_compute_hash_deterministic(self) -> None:
        # Golden value pins the exact digest so a change in sort_keys,
        # ensure_ascii, the __meta__ stripping, or the hash algorithm fails
        # loudly instead of silently altering org-migration integrity bundles.
        assert (
            _compute_hash(
                {
                    "__meta__": {"version": 1, "exported_at": "2024-01-01"},
                    "users": [{"id": "u1", "email": "a@b.com"}],
                }
            )
            == "60b826555c382021a9151e3b07327b3a458b917268619a9de745277f0a4e1941"
        )

    def test_verify_hash_ok(self) -> None:
        bundle: dict = {
            "__meta__": {"version": 1, "exported_at": "2024-01-01"},
            "organisation": {"id": "o1"},
        }
        bundle["__meta__"]["hash"] = _compute_hash(bundle)
        assert _verify_hash(bundle) is True

    def test_verify_hash_mismatch(self) -> None:
        bundle: dict = {
            "__meta__": {"version": 1, "exported_at": "2024-01-01", "hash": "wrong"},
            "organisation": {"id": "o1"},
        }
        assert _verify_hash(bundle) is False

    def test_verify_hash_missing_key(self) -> None:
        bundle: dict = {
            "__meta__": {"version": 1, "exported_at": "2024-01-01"},
            "organisation": {"id": "o1"},
        }
        assert _verify_hash(bundle) is False

    def test_compute_hash_excludes_existing_hash_key(self) -> None:
        # The stored hash must never feed back into itself: _compute_hash strips
        # the "hash" meta key, so recomputing over a bundle that already carries
        # its hash must yield the identical value.
        bundle: dict = {
            "__meta__": {"version": 1, "exported_at": "2024-01-01"},
            "organisation": {"id": "o1"},
        }
        without = _compute_hash(bundle)
        bundle["__meta__"]["hash"] = without
        assert _compute_hash(bundle) == without

    def test_compute_hash_unicode_stable(self) -> None:
        bundle: dict = {
            "__meta__": {"version": 1, "exported_at": "2024-01-01"},
            "users": [{"id": "u1", "display_name": "caf\u00e9 \u2014 snowman \u2603"}],
        }
        # Golden value pins the exact digest so a change in sort_keys,
        # ensure_ascii, or the hash algorithm fails loudly.
        assert _compute_hash(bundle) == "3a21638f66936987992d7c5636672904f849b21cd68004ef4a1d17610a8b31f6"
        assert len(_compute_hash(bundle)) == 64


# ── FK remapping ─────────────────────────────────────────────────────────────


class TestRemapFk:
    def test_remaps_mapped_fk_columns(self) -> None:
        id_map = {
            "11111111-1111-1111-1111-111111111111": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        }
        row = {
            "id": "11111111-1111-1111-1111-111111111111",
            "organisation_id": "22222222-2222-2222-2222-222222222222",
            "owner_team_id": "11111111-1111-1111-1111-111111111111",
            "created_by": None,
            "name": "prod",
        }
        result = _remap_fk(row, "stages", id_map)
        assert result["owner_team_id"] == uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        assert result["created_by"] is None
        assert result["name"] == "prod"
        assert result["id"] == "11111111-1111-1111-1111-111111111111"
        # Unmapped values pass through untouched (as raw strings, not UUIDs).
        assert result["organisation_id"] == "22222222-2222-2222-2222-222222222222"

    def test_unmapped_value_preserved(self) -> None:
        row = {"id": "u1", "owner_team_id": "99999999-9999-9999-9999-999999999999"}
        result = _remap_fk(row, "stages", {"11111111-1111-1111-1111-111111111111": "aaa"})
        assert result["owner_team_id"] == "99999999-9999-9999-9999-999999999999"

    def test_unknown_table_unchanged(self) -> None:
        row = {"id": "u1", "owner_team_id": "99999999-9999-9999-9999-999999999999"}
        assert _remap_fk(row, "no_such_table", {}) == row

    def test_original_row_not_mutated(self) -> None:
        id_map = {"11111111-1111-1111-1111-111111111111": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}
        row = {"id": "u1", "owner_team_id": "11111111-1111-1111-1111-111111111111"}
        _remap_fk(row, "stages", id_map)
        assert row["owner_team_id"] == "11111111-1111-1111-1111-111111111111"


# ── Bundle loading / writing ─────────────────────────────────────────────────


class TestLoadBundle:
    def test_loads_valid_bundle(self, tmp_path: Path) -> None:
        bundle: dict = {"__meta__": {"version": 1}, "organisation": {"id": "o1"}}
        bundle["__meta__"]["hash"] = _compute_hash(bundle)
        path = tmp_path / "bundle.json"
        path.write_text(json.dumps(bundle))
        assert _load_bundle(path) == bundle

    def test_missing_file_exits(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exc:
            _load_bundle(tmp_path / "missing.json")
        assert "not found" in str(exc.value)

    def test_invalid_json_exits(self, tmp_path: Path) -> None:
        path = tmp_path / "invalid.json"
        path.write_text("{not valid json")
        with pytest.raises(SystemExit) as exc:
            _load_bundle(path)
        assert "Failed to read import file" in str(exc.value)

    def test_hash_mismatch_exits(self, tmp_path: Path) -> None:
        path = tmp_path / "tampered.json"
        path.write_text(json.dumps({"__meta__": {"hash": "wrong"}, "organisation": {"id": "o1"}}))
        with pytest.raises(SystemExit) as exc:
            _load_bundle(path)
        assert "hash verification failed" in str(exc.value).lower()


class TestWriteBundle:
    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        out = tmp_path / "nested" / "dir" / "export.json"
        bundle = {"__meta__": {"version": 1}, "users": []}
        _write_bundle(bundle, out)
        assert out.exists()
        assert json.loads(out.read_text(encoding="utf-8")) == bundle

    def test_refuses_overwrite_without_force(self, tmp_path: Path) -> None:
        out = tmp_path / "exists.json"
        out.write_text("old")
        with pytest.raises(SystemExit) as exc:
            _write_bundle({"a": 1}, out)
        assert "already exists" in str(exc.value)
        assert out.read_text(encoding="utf-8") == "old"

    def test_overwrites_with_force(self, tmp_path: Path) -> None:
        out = tmp_path / "exists.json"
        out.write_text("old")
        _write_bundle({"a": 1}, out, force=True)
        assert json.loads(out.read_text(encoding="utf-8")) == {"a": 1}

    def test_existing_directory_path_rejected(self, tmp_path: Path) -> None:
        # A path that already exists (here, as a directory) must not be silently
        # overwritten without --force.
        with pytest.raises(SystemExit) as exc:
            _write_bundle({"a": 1}, tmp_path)
        assert "already exists" in str(exc.value)


# ── Export command tests ────────────────────────────────────────────────────


class TestExport:
    @patch("modulo.cli.migrate_org._do_export", new_callable=AsyncMock)
    @patch("modulo.cli.migrate_org._write_bundle")
    def test_export_basic(
        self,
        mock_write: MagicMock,
        mock_do_export: AsyncMock,
        tmp_path: Path,
    ) -> None:
        mock_do_export.return_value = {
            "__meta__": {"version": 1, "exported_at": "2024-01-01", "hash": "abc"},
            "organisation": {"id": "o1"},
        }
        output = tmp_path / "export.json"
        main(["export-org", "--org-id", "00000000-0000-0000-0000-000000000001", "--output", str(output)])
        mock_write.assert_called_once()
        assert mock_write.call_args[0][1] == output

    @patch("modulo.cli.migrate_org._do_export", new_callable=AsyncMock)
    def test_export_org_not_found(self, mock_do_export: AsyncMock) -> None:
        mock_do_export.side_effect = SystemExit("Organisation 00000000-0000-0000-0000-000000000001 not found")
        with pytest.raises(SystemExit) as exc:
            main(["export-org", "--org-id", "00000000-0000-0000-0000-000000000001", "--output", "out.json"])
        assert "not found" in str(exc.value)

    @patch("modulo.cli.migrate_org._do_export", new_callable=AsyncMock)
    def test_export_existing_output_without_force(self, mock_do_export: AsyncMock, tmp_path: Path) -> None:
        mock_do_export.return_value = {
            "__meta__": {"version": 1, "exported_at": "2024-01-01", "hash": "abc"},
            "organisation": {"id": "o1"},
        }
        output = tmp_path / "existing.json"
        output.write_text("old data")
        with pytest.raises(SystemExit) as exc:
            main(["export-org", "--org-id", "00000000-0000-0000-0000-000000000001", "--output", str(output)])
        assert "already exists" in str(exc.value)

    @patch("modulo.cli.migrate_org._do_export", new_callable=AsyncMock)
    @patch("modulo.cli.migrate_org._write_bundle")
    def test_export_existing_output_with_force(
        self,
        mock_write: MagicMock,
        mock_do_export: AsyncMock,
        tmp_path: Path,
    ) -> None:
        mock_do_export.return_value = {
            "__meta__": {"version": 1, "exported_at": "2024-01-01", "hash": "abc"},
            "organisation": {"id": "o1"},
        }
        output = tmp_path / "existing_force.json"
        output.write_text("old data")
        main(["export-org", "--org-id", "00000000-0000-0000-0000-000000000001", "--output", str(output), "--force"])
        mock_write.assert_called_once_with(mock_do_export.return_value, output, force=True)


# ── Import command tests ────────────────────────────────────────────────────


class TestImport:
    HASHED_BUNDLE: ClassVar[dict] = {
        "__meta__": {
            "version": 1,
            "exported_at": "2024-01-01",
        },
        "organisation": {"id": "o1"},
    }

    @staticmethod
    def _make_bundle_file(bundle: dict, path: Path) -> None:
        bundle["__meta__"]["hash"] = _compute_hash(bundle)
        path.write_text(json.dumps(bundle, indent=2))

    @patch("modulo.cli.migrate_org._do_import", new_callable=AsyncMock)
    def test_import_basic(self, mock_do_import: AsyncMock, tmp_path: Path) -> None:
        mock_do_import.return_value = {"created": 5, "overwritten": 0, "skipped": 0, "errors": 0}
        input_path = tmp_path / "bundle.json"
        self._make_bundle_file(dict(self.HASHED_BUNDLE), input_path)
        main(["import-org", "--org-id", "00000000-0000-0000-0000-000000000001", "--input", str(input_path)])
        mock_do_import.assert_called_once()

    def test_import_file_not_found(self) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["import-org", "--org-id", "00000000-0000-0000-0000-000000000001", "--input", "nonexistent.json"])
        assert "not found" in str(exc.value)

    def test_import_hash_mismatch_aborts(self, tmp_path: Path) -> None:
        input_path = tmp_path / "bad_bundle.json"
        input_path.write_text(json.dumps({"__meta__": {"hash": "wrong"}, "organisation": {"id": "o1"}}))
        with pytest.raises(SystemExit) as exc:
            main(["import-org", "--org-id", "00000000-0000-0000-0000-000000000001", "--input", str(input_path)])
        assert "hash verification failed" in str(exc.value).lower()

    @patch("modulo.cli.migrate_org._do_import", new_callable=AsyncMock)
    def test_import_skips_existing(self, mock_do_import: AsyncMock, tmp_path: Path) -> None:
        mock_do_import.return_value = {"created": 0, "overwritten": 0, "skipped": 10, "errors": 0}
        input_path = tmp_path / "skip_bundle.json"
        self._make_bundle_file(dict(self.HASHED_BUNDLE), input_path)
        main(["import-org", "--org-id", "00000000-0000-0000-0000-000000000001", "--input", str(input_path)])
        mock_do_import.assert_called_once()
