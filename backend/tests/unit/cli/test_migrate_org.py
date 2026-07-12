"""Tests for the modulo export-org / import-org CLI (argparse-based)."""

import json
import uuid
from pathlib import Path
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.cli.migrate_org import (
    _compute_hash,
    _parse_uuid,
    _serialise,
    _serialise_row,
    _verify_hash,
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
        assert _serialise({1, 2, 3}) == [1, 2, 3]

    def test_serialise_none(self) -> None:
        assert _serialise(None) is None


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


class TestHash:
    def test_compute_hash_deterministic(self) -> None:
        bundle = {
            "__meta__": {"version": 1, "exported_at": "2024-01-01"},
            "users": [{"id": "u1", "email": "a@b.com"}],
        }
        h1 = _compute_hash(bundle)
        h2 = _compute_hash(bundle)
        assert h1 == h2

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
