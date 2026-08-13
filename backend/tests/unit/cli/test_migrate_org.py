"""Tests for the modulo export-org / import-org CLI (argparse-based)."""

import json
import runpy
import uuid
from pathlib import Path
from typing import Any, ClassVar, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.cli.migrate_org import (
    ENTITY_ORDER,
    PAGE_SIZE,
    _compute_hash,
    _do_export,
    _do_import,
    _export_entity,
    _export_organisation,
    _load_bundle,
    _parse_uuid,
    _remap_fk,
    _serialise,
    _serialise_row,
    _validate_bundle,
    _verify_hash,
    _write_bundle,
    main,
)
from modulo.db.models import Account, Team
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
            _parse_uuid(12345, "test")  # type: ignore[arg-type]  # non-str is deliberate
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
        bundle: dict[str, Any] = {
            "__meta__": {"version": 1, "exported_at": "2024-01-01"},
            "organisation": {"id": "o1"},
        }
        bundle["__meta__"]["hash"] = _compute_hash(bundle)
        assert _verify_hash(bundle) is True

    def test_verify_hash_mismatch(self) -> None:
        bundle: dict[str, Any] = {
            "__meta__": {"version": 1, "exported_at": "2024-01-01", "hash": "wrong"},
            "organisation": {"id": "o1"},
        }
        assert _verify_hash(bundle) is False

    def test_verify_hash_missing_key(self) -> None:
        bundle: dict[str, Any] = {
            "__meta__": {"version": 1, "exported_at": "2024-01-01"},
            "organisation": {"id": "o1"},
        }
        assert _verify_hash(bundle) is False

    def test_compute_hash_excludes_existing_hash_key(self) -> None:
        # The stored hash must never feed back into itself: _compute_hash strips
        # the "hash" meta key, so recomputing over a bundle that already carries
        # its hash must yield the identical value.
        bundle: dict[str, Any] = {
            "__meta__": {"version": 1, "exported_at": "2024-01-01"},
            "organisation": {"id": "o1"},
        }
        without = _compute_hash(bundle)
        bundle["__meta__"]["hash"] = without
        assert _compute_hash(bundle) == without

    def test_compute_hash_unicode_stable(self) -> None:
        bundle: dict[str, Any] = {
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
        result = _remap_fk(row, "model_backends", id_map)
        assert result["owner_team_id"] == uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        assert result["created_by"] is None
        assert result["name"] == "prod"
        assert result["id"] == "11111111-1111-1111-1111-111111111111"
        # Unmapped values pass through untouched (as raw strings, not UUIDs).
        assert result["organisation_id"] == "22222222-2222-2222-2222-222222222222"

    def test_unmapped_value_preserved(self) -> None:
        row = {"id": "u1", "owner_team_id": "99999999-9999-9999-9999-999999999999"}
        result = _remap_fk(row, "model_backends", {"11111111-1111-1111-1111-111111111111": "aaa"})
        assert result["owner_team_id"] == "99999999-9999-9999-9999-999999999999"

    def test_unknown_table_unchanged(self) -> None:
        row = {"id": "u1", "owner_team_id": "99999999-9999-9999-9999-999999999999"}
        assert _remap_fk(row, "no_such_table", {}) == row

    def test_original_row_not_mutated(self) -> None:
        id_map = {"11111111-1111-1111-1111-111111111111": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}
        row = {"id": "u1", "owner_team_id": "11111111-1111-1111-1111-111111111111"}
        _remap_fk(row, "model_backends", id_map)
        assert row["owner_team_id"] == "11111111-1111-1111-1111-111111111111"


# ── Bundle loading / writing ─────────────────────────────────────────────────


class TestValidateBundle:
    VALID: ClassVar[dict[str, Any]] = {
        "__meta__": {"version": 1, "hash": "abc"},
        "organisation": {"id": "o1"},
        "users": [{"id": "u1", "email": "a@b.com"}],
        "teams": [{"id": "t1", "name": "Platform"}],
    }

    def test_valid_bundle_has_no_errors(self) -> None:
        assert _validate_bundle(dict(self.VALID)) == []

    def test_empty_bundle_has_no_errors(self) -> None:
        # A bundle that only carries meta + organisation (no entity tables yet)
        # is structurally fine — empty tables are valid.
        assert _validate_bundle({"__meta__": {"version": 1}, "organisation": {"id": "o1"}}) == []

    def test_non_object_root_reports_error(self) -> None:
        errors = _validate_bundle([1, 2, 3])
        assert len(errors) == 1
        assert "must be a JSON object" in errors[0]
        assert "list" in errors[0]

    def test_missing_meta_reports_error(self) -> None:
        errors = _validate_bundle({"organisation": {"id": "o1"}})
        assert any("__meta__" in e for e in errors)

    def test_missing_organisation_reports_error(self) -> None:
        errors = _validate_bundle({"__meta__": {"version": 1}})
        assert any("organisation" in e for e in errors)

    def test_table_not_a_list_reports_error(self) -> None:
        errors = _validate_bundle({"__meta__": {"version": 1}, "organisation": {"id": "o1"}, "users": {"u1": {}}})
        assert len(errors) == 1
        assert "'users' must be a JSON array" in errors[0]

    def test_row_not_a_dict_reports_error(self) -> None:
        errors = _validate_bundle(
            {"__meta__": {"version": 1}, "organisation": {"id": "o1"}, "users": [{"id": "u1"}, "not-a-row"]}
        )
        assert any("'users' row 1" in e and "must be a JSON object" in e for e in errors)

    def test_non_string_row_id_reports_error(self) -> None:
        errors = _validate_bundle(
            {"__meta__": {"version": 1}, "organisation": {"id": "o1"}, "users": [{"id": 123, "email": "a@b.com"}]}
        )
        assert any("'id' must be a string" in e for e in errors)

    def test_null_row_id_is_accepted(self) -> None:
        # A row without a persistent id is re-created fresh on import; export
        # never emits one, but null is a legitimate structural shape.
        assert (
            _validate_bundle(
                {"__meta__": {"version": 1}, "organisation": {"id": "o1"}, "users": [{"id": None, "email": "a@b.com"}]}
            )
            == []
        )

    def test_multiple_problems_accumulate(self) -> None:
        errors = _validate_bundle(
            {
                "teams": "not-a-list",
                "users": [{"id": "u1"}, {"id": 7}],
            }
        )
        assert len(errors) >= 3
        assert any("__meta__" in e for e in errors)
        assert any("organisation" in e for e in errors)
        assert any("'teams' must be a JSON array" in e for e in errors)
        assert any("'users' row 1 'id' must be a string" in e for e in errors)

    def test_unknown_top_level_keys_are_ignored(self) -> None:
        bundle = dict(self.VALID)
        bundle["not_an_entity"] = "ignored"
        assert _validate_bundle(bundle) == []


class TestLoadBundle:
    def test_loads_valid_bundle(self, tmp_path: Path) -> None:
        bundle: dict[str, Any] = {"__meta__": {"version": 1}, "organisation": {"id": "o1"}}
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

    def test_non_object_root_exits(self, tmp_path: Path) -> None:
        # A JSON array root used to crash with an AttributeError inside
        # _verify_hash; it must now exit with a structural error instead.
        path = tmp_path / "array.json"
        path.write_text(json.dumps([{"id": "o1"}]))
        with pytest.raises(SystemExit) as exc:
            _load_bundle(path)
        assert "invalid bundle structure" in str(exc.value)
        assert "must be a JSON object" in str(exc.value)

    def test_corrupt_table_structure_exits(self, tmp_path: Path) -> None:
        # Structurally corrupt but hash-consistent bundles are rejected before
        # any import work — validation runs ahead of hash verification.
        bundle: dict[str, Any] = {"__meta__": {"version": 1}, "organisation": {"id": "o1"}, "users": {"u1": {}}}
        bundle["__meta__"]["hash"] = _compute_hash(bundle)
        path = tmp_path / "corrupt.json"
        path.write_text(json.dumps(bundle))
        with pytest.raises(SystemExit) as exc:
            _load_bundle(path)
        assert "invalid bundle structure" in str(exc.value)
        assert "'users' must be a JSON array" in str(exc.value)

    def test_corrupt_row_id_exits(self, tmp_path: Path) -> None:
        bundle: dict[str, Any] = {
            "__meta__": {"version": 1},
            "organisation": {"id": "o1"},
            "users": [{"id": 123, "email": "a@b.com"}],
        }
        bundle["__meta__"]["hash"] = _compute_hash(bundle)
        path = tmp_path / "bad_id.json"
        path.write_text(json.dumps(bundle))
        with pytest.raises(SystemExit) as exc:
            _load_bundle(path)
        assert "invalid bundle structure" in str(exc.value)
        assert "'id' must be a string" in str(exc.value)

    def test_structural_error_precedes_hash_mismatch(self, tmp_path: Path) -> None:
        # A file that is both structurally invalid AND hash-mismatched reports
        # the structural problem first — a clearer diagnosis for corrupt input.
        path = tmp_path / "both_bad.json"
        path.write_text(json.dumps({"__meta__": {"hash": "wrong"}, "users": "not-a-list"}))
        with pytest.raises(SystemExit) as exc:
            _load_bundle(path)
        assert "invalid bundle structure" in str(exc.value)
        assert "hash verification failed" not in str(exc.value)


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

    def test_unwritable_path_exits(self, tmp_path: Path) -> None:
        # A regular file blocking the output's parent directory makes mkdir
        # fail, which must surface as a clean SystemExit rather than a raw OSError.
        blocker = tmp_path / "blocker.txt"
        blocker.write_text("x")
        out = blocker / "nested" / "export.json"
        with pytest.raises(SystemExit) as exc:
            _write_bundle({"a": 1}, out)
        assert "Failed to write export" in str(exc.value)


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
    HASHED_BUNDLE: ClassVar[dict[str, Any]] = {
        "__meta__": {
            "version": 1,
            "exported_at": "2024-01-01",
        },
        "organisation": {"id": "o1"},
    }

    @staticmethod
    def _make_bundle_file(bundle: dict[str, Any], path: Path) -> None:
        bundle["__meta__"]["hash"] = _compute_hash(bundle)
        path.write_text(json.dumps(bundle, indent=2))

    @patch("modulo.cli.migrate_org._do_import", new_callable=AsyncMock)
    def test_import_basic(self, mock_do_import: AsyncMock, tmp_path: Path) -> None:
        mock_do_import.return_value = {"created": 5, "overwritten": 0, "skipped": 0, "errors": 0}
        input_path = tmp_path / "bundle.json"
        self._make_bundle_file(dict(self.HASHED_BUNDLE), input_path)
        main(["import-org", "--org-id", "00000000-0000-0000-0000-000000000001", "--input", str(input_path)])
        mock_do_import.assert_called_once()
        called_bundle, called_org, called_strategy = mock_do_import.call_args.args
        assert called_bundle["organisation"]["id"] == "o1"
        assert called_bundle["__meta__"]["version"] == 1
        assert called_org == uuid.UUID("00000000-0000-0000-0000-000000000001")
        assert called_strategy == "skip"

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
        called_bundle, called_org, called_strategy = mock_do_import.call_args.args
        assert called_bundle["organisation"]["id"] == "o1"
        assert called_org == uuid.UUID("00000000-0000-0000-0000-000000000001")
        assert called_strategy == "skip"


# ── DB-layer helpers used by the export/import flow ─────────────────────────


class _Existing:
    """Minimal stand-in for a pre-existing ORM row already in the database."""

    def __init__(self, **kwargs: object) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


_UNSET = object()


class _ScalarResult:
    """Doubles for ``result.scalars()`` so ``.first()`` / ``.all()`` work."""

    def __init__(self, rows: list[Any] | None = None, first: object | None = _UNSET) -> None:
        self._rows = list(rows or [])
        self._first = first

    def scalars(self) -> "_ScalarResult":
        return self

    def first(self) -> object | None:
        if self._first is not _UNSET:
            return self._first
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return list(self._rows)


class _NestedTx:
    async def __aenter__(self) -> "Self":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _FakeSession:
    """Scripted async SQLAlchemy session for exercising the DB-layer helpers.

    ``execute()`` pops results from ``results`` in call order, so tests can
    script the exact sequence of "find existing" / "rename probe" queries the
    import loop makes.
    """

    def __init__(
        self,
        results: list[_ScalarResult] | None = None,
        *,
        flush_error: BaseException | None = None,
        execute_error: BaseException | None = None,
    ) -> None:
        self._queue = list(results or [])
        self.flush_error = flush_error
        self.execute_error = execute_error
        self.added: list[Any] = []
        self.flushed = 0
        self.committed = False

    def begin_nested(self) -> _NestedTx:
        return _NestedTx()

    async def execute(self, stmt: object) -> _ScalarResult:
        if self.execute_error is not None:
            raise self.execute_error
        if not self._queue:
            return _ScalarResult()
        return self._queue.pop(0)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flushed += 1
        if self.flush_error is not None:
            raise self.flush_error

    async def commit(self) -> None:
        self.committed = True


# ── _export_entity ──────────────────────────────────────────────────────────


class TestExportEntity:
    async def test_returns_serialised_rows(self, org_id: uuid.UUID) -> None:
        uid = uuid.uuid4()
        session = _FakeSession([_ScalarResult(rows=[MockModel(id=uid, name="Platform")]), _ScalarResult()])
        rows = await _export_entity(session, Team, org_id)
        assert rows == [{"id": str(uid), "name": "Platform"}]

    async def test_paginates_across_batches(self, org_id: uuid.UUID) -> None:
        first = [MockModel(id=uuid.uuid4(), name=f"t{i}") for i in range(PAGE_SIZE)]
        second = [
            MockModel(id=uuid.uuid4(), name=f"t{PAGE_SIZE}"),
            MockModel(id=uuid.uuid4(), name=f"t{PAGE_SIZE + 1}"),
        ]
        session = _FakeSession([_ScalarResult(rows=first), _ScalarResult(rows=second), _ScalarResult()])
        rows = await _export_entity(session, Team, org_id)
        assert len(rows) == PAGE_SIZE + 2

    async def test_empty_table_returns_empty_list(self, org_id: uuid.UUID) -> None:
        rows = await _export_entity(_FakeSession([_ScalarResult()]), Team, org_id)
        assert rows == []

    async def test_accounts_exported_without_org_filter(self, org_id: uuid.UUID) -> None:
        # Account has no organisation_id column (org membership is via
        # OrgMembership) — the export must not build a broken where clause.
        uid = uuid.uuid4()
        session = _FakeSession([_ScalarResult(rows=[MockModel(id=uid, email="a@b.com")]), _ScalarResult()])
        rows = await _export_entity(session, Account, org_id)
        assert rows == [{"id": str(uid), "email": "a@b.com"}]


# ── _export_organisation ────────────────────────────────────────────────────


class TestExportOrganisation:
    async def test_serialises_found_org(self, org_id: uuid.UUID) -> None:
        class _OrgSession:
            async def get(self, model: object, pk: object) -> MockModel:
                return MockModel(id=org_id, name="Acme", slug="acme")

        result = await _export_organisation(_OrgSession(), org_id)
        assert result == {"id": str(org_id), "name": "Acme", "slug": "acme"}

    async def test_missing_org_exits(self, org_id: uuid.UUID) -> None:
        class _OrgSession:
            async def get(self, model: object, pk: object) -> None:
                return None

        with pytest.raises(SystemExit) as exc:
            await _export_organisation(_OrgSession(), org_id)
        assert "not found" in str(exc.value)


# ── _do_export ──────────────────────────────────────────────────────────────


class TestDoExport:
    @patch("modulo.cli.migrate_org._export_entity", new_callable=AsyncMock)
    @patch("modulo.cli.migrate_org._export_organisation", new_callable=AsyncMock)
    async def test_builds_hashed_bundle(
        self,
        mock_export_org: AsyncMock,
        mock_export_entity: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        mock_export_org.return_value = {"id": str(org_id), "name": "Acme", "slug": "acme"}
        mock_export_entity.return_value = [{"id": "u1", "email": "a@b.com"}]
        session = MagicMock()
        mock_local = MagicMock()
        mock_local.__aenter__ = AsyncMock(return_value=session)
        mock_local.__aexit__ = AsyncMock(return_value=False)

        with patch("modulo.cli.migrate_org.AsyncSessionLocal", return_value=mock_local):
            bundle = await _do_export(org_id, Path("out.json"))

        assert bundle["organisation"] == {"id": str(org_id), "name": "Acme", "slug": "acme"}
        assert bundle["__meta__"]["version"] == 1
        assert bundle["__meta__"]["org_id"] == str(org_id)
        assert bundle["__meta__"]["org_name"] == "Acme"
        assert bundle["users"] == [{"id": "u1", "email": "a@b.com"}]
        assert bundle["__meta__"]["hash"] == _compute_hash(bundle)
        assert mock_export_entity.call_count == len(ENTITY_ORDER)

    @patch("modulo.cli.migrate_org._export_entity", new_callable=AsyncMock)
    @patch("modulo.cli.migrate_org._export_organisation", new_callable=AsyncMock)
    async def test_db_connection_failure_exits(
        self,
        mock_export_org: AsyncMock,
        mock_export_entity: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        mock_local = MagicMock()
        mock_local.__aenter__ = AsyncMock(side_effect=RuntimeError("db down"))
        mock_local.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("modulo.cli.migrate_org.AsyncSessionLocal", return_value=mock_local),
            pytest.raises(SystemExit) as exc,
        ):
            await _do_export(org_id, Path("out.json"))
        assert "Database connection failed" in str(exc.value)

    @patch("modulo.cli.migrate_org._export_entity", new_callable=AsyncMock)
    @patch("modulo.cli.migrate_org._export_organisation", new_callable=AsyncMock)
    async def test_cancellation_propagates(
        self,
        mock_export_org: AsyncMock,
        mock_export_entity: AsyncMock,
        org_id: uuid.UUID,
    ) -> None:
        # A task cancellation must not be swallowed and converted into the
        # generic "Database connection failed" SystemExit.
        import asyncio

        mock_local = MagicMock()
        mock_local.__aenter__ = AsyncMock(side_effect=asyncio.CancelledError())
        mock_local.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("modulo.cli.migrate_org.AsyncSessionLocal", return_value=mock_local),
            pytest.raises(asyncio.CancelledError),
        ):
            await _do_export(org_id, Path("out.json"))


# ── _do_import ──────────────────────────────────────────────────────────────


class TestDoImport:
    def _patched_session(
        self,
        results: list[_ScalarResult],
        *,
        flush_error: BaseException | None = None,
        execute_error: BaseException | None = None,
    ) -> tuple[_FakeSession, MagicMock]:
        session = _FakeSession(results, flush_error=flush_error, execute_error=execute_error)
        mock_local = MagicMock()
        mock_local.__aenter__ = AsyncMock(return_value=session)
        mock_local.__aexit__ = AsyncMock(return_value=False)
        return session, mock_local

    async def test_creates_new_records(self, org_id: uuid.UUID) -> None:
        session, mock_local = self._patched_session([_ScalarResult()])
        bundle = {"teams": [{"id": "t1", "name": "Platform", "description": "core team"}]}
        with patch("modulo.cli.migrate_org.AsyncSessionLocal", return_value=mock_local):
            counts = await _do_import(bundle, org_id, "skip")
        assert counts == {"created": 1, "overwritten": 0, "skipped": 0, "errors": 0}
        assert len(session.added) == 1
        assert session.committed is True
        assert session.added[0].name == "Platform"
        assert session.added[0].organisation_id == org_id

    async def test_creates_accounts_without_org_id(self, org_id: uuid.UUID) -> None:
        # Regression: Account has no organisation_id column, so the create
        # path must not inject one (mirrors the guard in the click-based CLI).
        session, mock_local = self._patched_session([_ScalarResult()])
        bundle = {"users": [{"id": "u1", "email": "a@b.com", "display_name": "A"}]}
        with patch("modulo.cli.migrate_org.AsyncSessionLocal", return_value=mock_local):
            counts = await _do_import(bundle, org_id, "skip")
        assert counts["created"] == 1
        assert counts["errors"] == 0
        assert isinstance(session.added[0], Account)
        assert not hasattr(session.added[0], "organisation_id")

    async def test_skip_existing_records(self, org_id: uuid.UUID) -> None:
        existing = _Existing(id=uuid.uuid4(), name="Platform")
        session, mock_local = self._patched_session([_ScalarResult(first=existing)])
        bundle = {"teams": [{"id": "t1", "name": "Platform", "description": "core team"}]}
        with patch("modulo.cli.migrate_org.AsyncSessionLocal", return_value=mock_local):
            counts = await _do_import(bundle, org_id, "skip")
        assert counts == {"created": 0, "overwritten": 0, "skipped": 1, "errors": 0}
        assert session.added == []

    async def test_overwrite_existing_records(self, org_id: uuid.UUID) -> None:
        existing: Any = _Existing(id=uuid.uuid4(), name="Platform", description="old")
        _, mock_local = self._patched_session([_ScalarResult(first=existing)])
        bundle = {"teams": [{"id": "t1", "name": "Platform", "description": "new desc"}]}
        with patch("modulo.cli.migrate_org.AsyncSessionLocal", return_value=mock_local):
            counts = await _do_import(bundle, org_id, "overwrite")
        assert counts == {"created": 0, "overwritten": 1, "skipped": 0, "errors": 0}
        assert existing.description == "new desc"

    async def test_rename_existing_records(self, org_id: uuid.UUID) -> None:
        existing = _Existing(id=uuid.uuid4(), name="Platform")
        session, mock_local = self._patched_session([_ScalarResult(first=existing), _ScalarResult()])
        bundle = {"teams": [{"id": "t1", "name": "Platform", "description": "core team"}]}
        with patch("modulo.cli.migrate_org.AsyncSessionLocal", return_value=mock_local):
            counts = await _do_import(bundle, org_id, "rename")
        assert counts["created"] == 1
        assert counts["errors"] == 0
        assert session.added[0].name == "Platform_imported"

    async def test_rename_exhaustion_exits(self, org_id: uuid.UUID) -> None:
        existing = _Existing(id=uuid.uuid4(), name="Platform")
        # Every candidate name from Platform_imported .. Platform_imported_9999
        # is already taken, so the probe loop runs out of candidates.
        results = [_ScalarResult(first=existing)]
        results.extend(_ScalarResult(first=existing) for _ in range(9999))
        _, mock_local = self._patched_session(results)
        bundle = {"teams": [{"id": "t1", "name": "Platform", "description": "core team"}]}
        with (
            patch("modulo.cli.migrate_org.AsyncSessionLocal", return_value=mock_local),
            pytest.raises(SystemExit) as exc,
        ):
            await _do_import(bundle, org_id, "rename")
        assert "Could not find available name" in str(exc.value)

    async def test_row_error_is_counted(self, org_id: uuid.UUID) -> None:
        session, mock_local = self._patched_session([_ScalarResult()], flush_error=RuntimeError("boom"))
        bundle = {"teams": [{"id": "t1", "name": "Platform", "description": "core team"}]}
        with patch("modulo.cli.migrate_org.AsyncSessionLocal", return_value=mock_local):
            counts = await _do_import(bundle, org_id, "skip")
        assert counts == {"created": 0, "overwritten": 0, "skipped": 0, "errors": 1}
        assert session.committed is True

    async def test_row_cancellation_propagates(self, org_id: uuid.UUID) -> None:
        import asyncio

        _, mock_local = self._patched_session([_ScalarResult()], flush_error=asyncio.CancelledError())
        bundle = {"teams": [{"id": "t1", "name": "Platform", "description": "core team"}]}
        with (
            patch("modulo.cli.migrate_org.AsyncSessionLocal", return_value=mock_local),
            pytest.raises(asyncio.CancelledError),
        ):
            await _do_import(bundle, org_id, "skip")

    async def test_db_connection_cancellation_propagates(self, org_id: uuid.UUID) -> None:
        import asyncio

        mock_local = MagicMock()
        mock_local.__aenter__ = AsyncMock(side_effect=asyncio.CancelledError())
        mock_local.__aexit__ = AsyncMock(return_value=False)
        bundle: dict[str, Any] = {"teams": []}
        with (
            patch("modulo.cli.migrate_org.AsyncSessionLocal", return_value=mock_local),
            pytest.raises(asyncio.CancelledError),
        ):
            await _do_import(bundle, org_id, "skip")

    async def test_db_connection_failure_exits(self, org_id: uuid.UUID) -> None:
        mock_local = MagicMock()
        mock_local.__aenter__ = AsyncMock(side_effect=RuntimeError("db down"))
        mock_local.__aexit__ = AsyncMock(return_value=False)
        bundle: dict[str, Any] = {"teams": []}
        with (
            patch("modulo.cli.migrate_org.AsyncSessionLocal", return_value=mock_local),
            pytest.raises(SystemExit) as exc,
        ):
            await _do_import(bundle, org_id, "skip")
        assert "Database connection failed" in str(exc.value)


# ── Module guard ────────────────────────────────────────────────────────────


class TestModuleMain:
    def test_main_guard_parses_args(self) -> None:
        # Running the module as __main__ with no subcommand must exit(2)
        # rather than traceback, exercising the `if __name__ == "__main__"`
        # guard.
        from modulo.cli import migrate_org as _mod

        with pytest.raises(SystemExit) as exc:
            runpy.run_path(str(Path(_mod.__file__)), run_name="__main__")
        assert exc.value.code == 2
