"""Tests for the modulo-migrate CLI tool."""

import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click import ClickException
from click.testing import CliRunner

from modulo.cli.migrate import (
    _collect_org_data,
    _compute_export_hash,
    _group_records,
    _hash_record,
    _import_org_data,
    _parse_uuid,
    _read_jsonl,
    _remap_fk_row,
    _resolve_admin_auth,
    _serialise_row,
    _verify_admin_access,
    _verify_export,
    _write_jsonl,
    cli,
)
from tests.unit.cli.conftest import MockModel

# ── Pure function tests (no mocking needed) ──────────────────────────────────


class TestSerialisation:
    def test_serialise_row_handles_uuids(self) -> None:
        uid = uuid.uuid4()
        row = MockModel(id=uid, name="hello")
        result = _serialise_row(row)
        assert result["id"] == str(uid)
        assert result["name"] == "hello"

    def test_serialise_row_handles_datetime(self) -> None:
        from datetime import UTC, datetime

        dt = datetime.now(UTC)
        row = MockModel(ts=dt)
        result = _serialise_row(row)
        assert result["ts"] == dt.isoformat()

    def test_serialise_row_handles_bytes(self) -> None:
        row = MockModel(blob=b"\x00\xff")
        result = _serialise_row(row)
        assert result["blob"] == "00ff"


class TestHashRecord:
    def test_hash_record_deterministic(self) -> None:
        # Golden value pins the exact digest so a change in sort_keys,
        # ensure_ascii, or the hash algorithm fails loudly instead of silently
        # altering migration integrity hashes.
        assert _hash_record({"a": 1, "b": "x"}) == "ce5c626fb40307427cf323b5c307a3ea230856fa4bad676eaaa2577b5a857a85"

    def test_hash_record_different_inputs(self) -> None:
        assert _hash_record({"a": 1}) != _hash_record({"a": 2})


class TestComputeExportHash:
    def test_empty_bundle(self) -> None:
        h = _compute_export_hash({})
        assert isinstance(h, str)
        assert len(h) == 64

    def test_with_rows(self) -> None:
        # "users" is not an _EXPORT_TABLES entry and would make this vacuous;
        # use a real table so the rows actually feed the hash.
        bundle = {"accounts": [{"id": "u1", "name": "alice"}, {"id": "u2", "name": "bob"}]}
        h = _compute_export_hash(bundle)
        assert isinstance(h, str)
        assert len(h) == 64

    def test_rows_affect_hash(self) -> None:
        assert _compute_export_hash({"accounts": [{"id": "u1"}]}) != _compute_export_hash({"accounts": [{"id": "u2"}]})

    def test_order_independent(self) -> None:
        # Rows are sorted by id before hashing, so bundle insertion order must
        # not change the export hash.
        bundle = {"accounts": [{"id": "u1", "name": "a"}, {"id": "u2", "name": "b"}]}
        shuffled = {"accounts": [{"id": "u2", "name": "b"}, {"id": "u1", "name": "a"}]}
        assert _compute_export_hash(bundle) == _compute_export_hash(shuffled)

    def test_only_export_tables_are_hashed(self) -> None:
        # Unknown keys are ignored by _compute_export_hash.
        assert _compute_export_hash({"no_such_table": [{"id": "u1"}]}) == _compute_export_hash({})


class TestGroupRecords:
    def test_groups_by_table(self) -> None:
        records = [
            {"__table__": "users", "id": "1", "data": {}},
            {"__table__": "users", "id": "2", "data": {}},
            {"__table__": "pipelines", "id": "3", "data": {}},
        ]
        groups = _group_records(records)
        assert len(groups["users"]) == 2
        assert len(groups["pipelines"]) == 1

    def test_no_table_key(self) -> None:
        records = [{"id": "1"}]
        groups = _group_records(records)
        assert "__orphan__" not in groups
        assert not groups

    def test_empty_records(self) -> None:
        assert not _group_records([])


class TestReadJsonl:
    async def test_reads_meta_and_records(self, tmp_path: Path) -> None:
        path = tmp_path / "export.jsonl"
        path.write_text(
            json.dumps({"__meta__": {"version": 1, "export_hash": "abc"}})
            + "\n"
            + json.dumps({"__table__": "users", "id": "1", "data": {"name": "a"}})
            + "\n"
            + json.dumps({"__table__": "users", "id": "2", "data": {"name": "b"}})
            + "\n"
        )

        meta, records = await _read_jsonl(path)
        assert meta["version"] == 1
        assert meta["export_hash"] == "abc"
        assert len(records) == 2

    async def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("")

        meta, records = await _read_jsonl(path)
        assert meta == {}
        assert records == []

    async def test_blank_lines_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "blank.jsonl"
        path.write_text("\n\n" + json.dumps({"__table__": "accounts", "id": "1", "data": {}}) + "\n\n")
        meta, records = await _read_jsonl(path)
        assert meta == {}
        assert len(records) == 1

    async def test_record_before_meta(self, tmp_path: Path) -> None:
        # The first line not being a header must not swallow the record.
        path = tmp_path / "no_meta.jsonl"
        path.write_text(json.dumps({"__table__": "accounts", "id": "1", "data": {}}) + "\n")
        meta, records = await _read_jsonl(path)
        assert meta == {}
        assert len(records) == 1


class TestReadJsonlErrors:
    async def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ClickException, match="Input file not found"):
            await _read_jsonl(tmp_path / "missing.jsonl")

    async def test_invalid_jsonl_line_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text("{not valid json\n")
        with pytest.raises(ClickException, match="Invalid JSONL line"):
            await _read_jsonl(path)


class TestParseUuid:
    def test_valid(self) -> None:
        uid = uuid.uuid4()
        assert _parse_uuid(str(uid), "test") == uid

    def test_invalid_raises_click_exception(self) -> None:
        with pytest.raises(ClickException, match="Invalid organisation ID"):
            _parse_uuid("not-a-uuid", "organisation ID")

    def test_non_string_raises_click_exception(self) -> None:
        with pytest.raises(ClickException, match="Invalid"):
            _parse_uuid(12345, "organisation ID")


class TestWriteJsonl:
    def test_writes_header_and_records(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        bundle = {
            "accounts": [{"id": "u1", "name": "alice"}],
            "exported_at": "2024-01-01T00:00:00",
        }
        hashes = _write_jsonl(bundle, path)
        assert "__export__" in hashes
        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2  # header + 1 record
        header = json.loads(lines[0])
        assert "__meta__" in header


class TestVerifyExport:
    def test_verify_ok(self) -> None:
        meta = {"export_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"}
        records: list[dict] = []
        result = _verify_export(meta, records)
        assert result is True

    def test_verify_mismatch(self) -> None:
        meta = {"export_hash": "aaa"}
        records = [{"__table__": "users", "id": "1", "__hash__": _hash_record({"id": "1"})}]
        result = _verify_export(meta, records)
        assert result is False


class TestVerifyExportRoundtrip:
    async def test_roundtrip_ok(self, tmp_path: Path) -> None:
        # Write a bundle, read it back, and confirm verification recomputes the
        # same export hash from the per-row __hash__ fields.
        bundle = {
            "accounts": [
                {"id": "u1", "name": "alice"},
                {"id": "u2", "name": "bob"},
            ],
            "exported_at": "2024-01-01T00:00:00+00:00",
        }
        path = tmp_path / "export.jsonl"
        _write_jsonl(bundle, path)
        meta, records = await _read_jsonl(path)
        assert meta["export_hash"] is not None
        assert _verify_export(meta, records) is True

    async def test_roundtrip_tampered_row_hash_fails(self, tmp_path: Path) -> None:
        bundle = {
            "accounts": [{"id": "u1", "name": "alice"}],
            "exported_at": "2024-01-01T00:00:00+00:00",
        }
        path = tmp_path / "export.jsonl"
        _write_jsonl(bundle, path)
        meta, records = await _read_jsonl(path)
        records[0]["__hash__"] = "0" * 64
        assert _verify_export(meta, records) is False


# ── FK row remapping ─────────────────────────────────────────────────────────


class TestRemapFkRow:
    def test_remaps_mapped_values(self) -> None:
        id_map = {"11111111-1111-1111-1111-111111111111": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}
        row = {
            "id": "11111111-1111-1111-1111-111111111111",
            "organisation_id": "org-1",
            "owner_team_id": "11111111-1111-1111-1111-111111111111",
            "created_at": "2024-01-01",
            "name": "prod",
        }
        _remap_fk_row(row, id_map)
        # id / organisation_id / created_at are excluded from remapping.
        assert row["id"] == "11111111-1111-1111-1111-111111111111"
        assert row["organisation_id"] == "org-1"
        assert row["created_at"] == "2024-01-01"
        assert row["owner_team_id"] == uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    def test_unmapped_values_untouched(self) -> None:
        row = {"owner_team_id": "22222222-2222-2222-2222-222222222222", "name": "prod"}
        _remap_fk_row(row, {"11111111-1111-1111-1111-111111111111": "x"})
        assert row["owner_team_id"] == "22222222-2222-2222-2222-222222222222"

    def test_none_values_untouched(self) -> None:
        row = {"owner_team_id": None, "created_by": None}
        _remap_fk_row(row, {"None": "x"})
        assert row["owner_team_id"] is None
        assert row["created_by"] is None


# ── Auth tests ───────────────────────────────────────────────────────────────


class TestAuth:
    def test_cli_requires_auth(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["export-org", "00000000-0000-0000-0000-000000000001"])
        assert result.exit_code != 0
        assert "Admin authentication required" in result.output

    @patch("modulo.cli.migrate.decode_principal")
    def test_cli_auth_with_token(self, mock_decode: MagicMock) -> None:
        from modulo.auth.jwt import AuthenticatedPrincipal

        mock_decode.return_value = AuthenticatedPrincipal(
            username="admin",
            organisation_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            account_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
            org_role="admin",
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["--token", "fake.jwt.token", "export-org", "00000000-0000-0000-0000-000000000001"])
        # Will fail later due to DB, but auth should pass
        assert "Admin authentication required" not in result.output


class TestResolveAdminAuth:
    def test_no_token_no_env_returns_none(self) -> None:
        with patch.dict("os.environ", {"MODULO_ADMIN_SECRET": ""}):
            assert _resolve_admin_auth(None) is None

    def test_env_secret_uses_marker(self) -> None:
        with patch.dict("os.environ", {"MODULO_ADMIN_SECRET": "s3cret"}):
            assert _resolve_admin_auth(None) == "__admin_secret__"

    def test_non_admin_token_rejected(self) -> None:
        with (
            patch("modulo.cli.migrate.decode_principal") as mock_decode,
            patch("modulo.cli.migrate.get_settings") as mock_settings,
        ):
            mock_decode.return_value = SimpleNamespace(org_role="member", user_id="u1")
            mock_settings.return_value = MagicMock(secret_key="key")
            with pytest.raises(ClickException, match="not an admin"):
                _resolve_admin_auth("some.jwt.token")

    def test_invalid_token_wrapped_as_click_exception(self) -> None:
        with (
            patch("modulo.cli.migrate.decode_principal", side_effect=RuntimeError("bad token")),
            patch("modulo.cli.migrate.get_settings") as mock_settings,
        ):
            mock_settings.return_value = MagicMock(secret_key="key")
            with pytest.raises(ClickException, match="Invalid admin JWT"):
                _resolve_admin_auth("garbage.jwt.token")


class TestVerifyAdminAccess:
    async def test_admin_secret_bypasses_db(self, org_id: uuid.UUID) -> None:
        session = MagicMock()
        await _verify_admin_access(session, org_id, "__admin_secret__")
        session.execute.assert_not_called()

    async def test_account_not_found_raises(self, org_id: uuid.UUID) -> None:
        with patch("modulo.cli.migrate.get_account_by_id", new_callable=AsyncMock) as mock_account:
            mock_account.return_value = None
            with pytest.raises(ClickException, match="Admin account not found"):
                await _verify_admin_access(MagicMock(), org_id, str(uuid.uuid4()))

    async def test_non_member_raises(self, org_id: uuid.UUID) -> None:
        account = MagicMock(id=uuid.uuid4())
        with (
            patch("modulo.cli.migrate.get_account_by_id", new_callable=AsyncMock) as mock_account,
            patch("modulo.cli.migrate.get_membership_by_account_and_org", new_callable=AsyncMock) as mock_membership,
        ):
            mock_account.return_value = account
            mock_membership.return_value = None
            with pytest.raises(ClickException, match="does not belong"):
                await _verify_admin_access(MagicMock(), org_id, str(account.id))

    async def test_non_admin_role_raises(self, org_id: uuid.UUID) -> None:
        account = MagicMock(id=uuid.uuid4())
        with (
            patch("modulo.cli.migrate.get_account_by_id", new_callable=AsyncMock) as mock_account,
            patch("modulo.cli.migrate.get_membership_by_account_and_org", new_callable=AsyncMock) as mock_membership,
        ):
            mock_account.return_value = account
            mock_membership.return_value = MagicMock(role="runner")
            with pytest.raises(ClickException, match="admin-level"):
                await _verify_admin_access(MagicMock(), org_id, str(account.id))


# ── Export command tests ─────────────────────────────────────────────────────


class TestExportOrg:
    @patch("modulo.cli.migrate.decode_principal")
    @patch("modulo.cli.migrate.AsyncSessionLocal")
    @patch("modulo.cli.migrate.get_organisation")
    @patch("modulo.cli.migrate.get_account_by_id", new_callable=AsyncMock)
    @patch("modulo.cli.migrate.get_membership_by_account_and_org")
    def test_export_basic(
        self,
        mock_get_membership: MagicMock,
        mock_get_account: AsyncMock,
        mock_get_org: MagicMock,
        mock_session_local: MagicMock,
        mock_decode: MagicMock,
        org_id: uuid.UUID,
        admin_user_id: uuid.UUID,
        tmp_path: Path,
    ) -> None:
        from modulo.auth.jwt import AuthenticatedPrincipal

        mock_decode.return_value = AuthenticatedPrincipal(
            username="admin",
            organisation_id=org_id,
            account_id=admin_user_id,
            org_role="admin",
        )
        mock_admin = MagicMock()
        mock_admin.org_role = "admin"
        mock_admin.organisation_id = org_id
        mock_get_account.return_value = mock_admin
        mock_get_membership.return_value = MagicMock(role="admin")

        mock_org = MockModel(id=org_id, name="Test Org", slug="test-org", status="active")
        mock_get_org.return_value = mock_org

        mock_session = AsyncMock()

        class FakeResult:
            def scalars(self):
                return self

            def all(self):
                return []

        async def fake_execute(_stmt):
            return FakeResult()

        mock_session.execute = fake_execute
        mock_session.__aenter__.return_value = mock_session

        mock_session_local.return_value.__aenter__.return_value = mock_session

        output_path = tmp_path / "export.jsonl"
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--token",
                "fake.jwt.token",
                "export-org",
                str(org_id),
                "--output",
                str(output_path),
            ],
        )
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "Exported" in result.output
        assert output_path.exists()

    @patch("modulo.cli.migrate.decode_principal")
    @patch("modulo.cli.migrate.AsyncSessionLocal")
    @patch("modulo.cli.migrate.get_organisation")
    @patch("modulo.cli.migrate.get_account_by_id", new_callable=AsyncMock)
    @patch("modulo.cli.migrate.get_membership_by_account_and_org")
    def test_export_org_not_found(
        self,
        mock_get_membership: MagicMock,
        mock_get_account: AsyncMock,
        mock_get_org: MagicMock,
        mock_session_local: MagicMock,
        mock_decode: MagicMock,
        org_id: uuid.UUID,
        admin_user_id: uuid.UUID,
        tmp_path: Path,
    ) -> None:
        from modulo.auth.jwt import AuthenticatedPrincipal

        mock_decode.return_value = AuthenticatedPrincipal(
            username="admin",
            organisation_id=org_id,
            account_id=admin_user_id,
            org_role="admin",
        )
        mock_admin = MagicMock()
        mock_admin.org_role = "admin"
        mock_admin.organisation_id = org_id
        mock_get_account.return_value = mock_admin
        mock_get_membership.return_value = MagicMock(role="admin")
        mock_get_org.return_value = None

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session_local.return_value.__aenter__.return_value = mock_session

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--token",
                "fake.jwt.token",
                "export-org",
                str(org_id),
                "--output",
                str(tmp_path / "out.jsonl"),
            ],
        )
        assert result.exit_code != 0
        assert "not found" in result.output


# ── Paginated collection tests ──────────────────────────────────────────────


class TestCollectOrgDataPagination:
    async def test_paginates_large_tables(self, org_id: uuid.UUID) -> None:
        from modulo.cli.migrate import _PAGE_SIZE, _collect_org_data

        async def fake_execute(_stmt: object) -> object:
            calls.append(1)
            page = (len(calls) - 1) % 3
            if page == 0:
                return FakeResult([MockModel(id=uuid.UUID(int=i)) for i in range(1, _PAGE_SIZE + 1)])
            if page == 1:
                return FakeResult([MockModel(id=uuid.UUID(int=i)) for i in range(_PAGE_SIZE + 1, _PAGE_SIZE * 2 + 1)])
            return FakeResult([])

        class FakeResult:
            def __init__(self, rows: list[MockModel]) -> None:
                self._rows = rows

            def scalars(self) -> "FakeResult":
                return self

            def all(self) -> list[MockModel]:
                return self._rows

        calls: list[int] = []
        session = MagicMock()
        session.execute = fake_execute

        with patch("modulo.cli.migrate.get_organisation", new_callable=AsyncMock) as mock_get_org:
            mock_get_org.return_value = MockModel(id=org_id, name="Test Org")
            bundle = await _collect_org_data(session, org_id)

        table_count = 7  # one entry per table in _MODEL_MAP
        assert len(calls) == table_count * 3  # two non-empty pages + one empty terminator per table
        for table in ("accounts", "pipelines", "runs", "audit_events"):
            assert len(bundle[table]) == _PAGE_SIZE * 2
            ids = [int(row["id"].replace("-", ""), 16) for row in bundle[table]]
            assert ids == sorted(ids)


class TestCollectOrgDataFlags:
    async def test_pipelines_only_and_users_only_mutually_exclusive(self, org_id: uuid.UUID) -> None:
        with patch("modulo.cli.migrate.get_organisation", new_callable=AsyncMock) as mock_get_org:
            mock_get_org.return_value = MockModel(id=org_id, name="Org")
            session = MagicMock()
            with pytest.raises(ClickException, match="mutually exclusive"):
                await _collect_org_data(session, org_id, pipelines_only=True, users_only=True)
            # The validation must fire before any table queries are issued.
            session.execute.assert_not_called()


# ── Import command tests ─────────────────────────────────────────────────────


class TestImportOrg:
    @patch("modulo.cli.migrate.decode_principal")
    @patch("modulo.cli.migrate.AsyncSessionLocal")
    @patch("modulo.cli.migrate.get_organisation")
    @patch("modulo.cli.migrate.get_account_by_id", new_callable=AsyncMock)
    @patch("modulo.cli.migrate.get_membership_by_account_and_org")
    def test_import_basic(
        self,
        mock_get_membership: MagicMock,
        mock_get_account: AsyncMock,
        mock_get_org: MagicMock,
        mock_session_local: MagicMock,
        mock_decode: MagicMock,
        org_id: uuid.UUID,
        admin_user_id: uuid.UUID,
        tmp_path: Path,
    ) -> None:
        from modulo.auth.jwt import AuthenticatedPrincipal

        mock_decode.return_value = AuthenticatedPrincipal(
            username="admin",
            organisation_id=org_id,
            account_id=admin_user_id,
            org_role="admin",
        )
        mock_admin = MagicMock()
        mock_admin.org_role = "admin"
        mock_admin.organisation_id = org_id
        mock_get_account.return_value = mock_admin
        mock_get_membership.return_value = MagicMock(role="admin")
        mock_get_org.return_value = MagicMock()

        mock_session = AsyncMock()
        mock_session.execute.return_value.scalar_one_or_none.return_value = None
        mock_session.__aenter__.return_value = mock_session
        mock_session_local.return_value.__aenter__.return_value = mock_session

        _empty_hash = hashlib.sha256().hexdigest()
        input_path = tmp_path / "import.jsonl"
        input_path.write_text(json.dumps({"__meta__": {"version": 1, "export_hash": _empty_hash}}) + "\n")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--token",
                "fake.jwt.token",
                "import-org",
                str(org_id),
                "--input",
                str(input_path),
            ],
        )
        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "Loaded" in result.output
        assert "Import complete" in result.output

    @patch("modulo.cli.migrate.decode_principal")
    @patch("modulo.cli.migrate.AsyncSessionLocal")
    @patch("modulo.cli.migrate.get_organisation")
    @patch("modulo.cli.migrate.get_account_by_id", new_callable=AsyncMock)
    @patch("modulo.cli.migrate.get_membership_by_account_and_org")
    def test_import_with_conflict_strategies(
        self,
        mock_get_membership: MagicMock,
        mock_get_account: AsyncMock,
        mock_get_org: MagicMock,
        mock_session_local: MagicMock,
        mock_decode: MagicMock,
        org_id: uuid.UUID,
        admin_user_id: uuid.UUID,
        tmp_path: Path,
    ) -> None:
        from modulo.auth.jwt import AuthenticatedPrincipal

        mock_decode.return_value = AuthenticatedPrincipal(
            username="admin",
            organisation_id=org_id,
            account_id=admin_user_id,
            org_role="admin",
        )
        mock_admin = MagicMock()
        mock_admin.org_role = "admin"
        mock_admin.organisation_id = org_id
        mock_get_account.return_value = mock_admin
        mock_get_membership.return_value = MagicMock(role="admin")
        mock_get_org.return_value = MagicMock()

        mock_session = AsyncMock()
        mock_session.execute.return_value.scalar_one_or_none.return_value = None
        mock_session.__aenter__.return_value = mock_session
        mock_session_local.return_value.__aenter__.return_value = mock_session

        _empty_hash = hashlib.sha256().hexdigest()
        input_path = tmp_path / "import_skip.jsonl"
        input_path.write_text(json.dumps({"__meta__": {"version": 1, "export_hash": _empty_hash}}) + "\n")

        for strategy in ["skip", "overwrite", "merge"]:
            runner = CliRunner()
            result = runner.invoke(
                cli,
                [
                    "--token",
                    "fake.jwt.token",
                    "import-org",
                    str(org_id),
                    "--input",
                    str(input_path),
                    "--on-conflict",
                    strategy,
                ],
            )
            assert result.exit_code == 0, f"Strategy '{strategy}' failed: {result.output}"


class _StubMigrateTable:
    """Minimal __table__ stand-in exposing primary_key.columns.keys()."""

    primary_key = SimpleNamespace(columns=SimpleNamespace(keys=lambda: ["id"]))


class _StubMigrateAccount:
    """Account stand-in that accepts arbitrary attributes like a mapped model."""

    __table__ = _StubMigrateTable()
    organisation_id = None
    email = None
    display_name = None

    def __init__(self, **kwargs: object) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestImportOrgData:
    """Unit coverage for the core _import_org_data conflict-resolution logic."""

    _RECORD: ClassVar[dict[str, object]] = {
        "__table__": "accounts",
        "id": "u1",
        "data": {"id": "u1", "email": "new@x.com"},
    }

    def _session(self) -> AsyncMock:
        # begin_nested() must return an async context manager (not the coroutine
        # an AsyncMock attribute would return) for `async with` to work.
        session = AsyncMock()

        class _Nested:
            async def __aenter__(self) -> AsyncMock:
                return session

            async def __aexit__(self, *exc: object) -> bool:
                return False

        session.begin_nested = lambda: _Nested()
        # add() is a synchronous call in the source; an AsyncMock would return an
        # unawaited coroutine and trip the repo's error::RuntimeWarning filter.
        session.add = MagicMock()
        return session

    @patch("modulo.cli.migrate._MODEL_MAP", {"accounts": _StubMigrateAccount})
    @patch("modulo.cli.migrate._find_existing_row", new_callable=AsyncMock)
    async def test_creates_new_row(self, mock_find: AsyncMock, org_id: uuid.UUID) -> None:
        mock_find.return_value = None
        session = self._session()
        counts = await _import_org_data(session, org_id, [dict(self._RECORD)], "skip")
        assert counts == {"created": 1, "skipped": 0, "overwritten": 0, "errors": 0}
        added = session.add.call_args[0][0]
        assert added.email == "new@x.com"
        # The source organisation_id is stripped and replaced by the target org.
        assert added.organisation_id == org_id
        session.flush.assert_awaited()

    @patch("modulo.cli.migrate._MODEL_MAP", {"accounts": _StubMigrateAccount})
    @patch("modulo.cli.migrate._find_existing_row", new_callable=AsyncMock)
    async def test_skip_strategy_leaves_existing(self, mock_find: AsyncMock, org_id: uuid.UUID) -> None:
        existing = _StubMigrateAccount(id=uuid.UUID("11111111-1111-1111-1111-111111111111"), email="old@x.com")
        mock_find.return_value = existing
        session = self._session()
        counts = await _import_org_data(session, org_id, [dict(self._RECORD)], "skip")
        assert counts == {"created": 0, "skipped": 1, "overwritten": 0, "errors": 0}
        session.add.assert_not_called()
        assert existing.email == "old@x.com"

    @patch("modulo.cli.migrate._MODEL_MAP", {"accounts": _StubMigrateAccount})
    @patch("modulo.cli.migrate._find_existing_row", new_callable=AsyncMock)
    async def test_overwrite_strategy_updates_existing(self, mock_find: AsyncMock, org_id: uuid.UUID) -> None:
        existing = _StubMigrateAccount(id=uuid.UUID("11111111-1111-1111-1111-111111111111"), email="old@x.com")
        mock_find.return_value = existing
        session = self._session()
        counts = await _import_org_data(session, org_id, [dict(self._RECORD)], "overwrite")
        assert counts == {"created": 0, "skipped": 0, "overwritten": 1, "errors": 0}
        assert existing.email == "new@x.com"

    @patch("modulo.cli.migrate._MODEL_MAP", {"accounts": _StubMigrateAccount})
    @patch("modulo.cli.migrate._find_existing_row", new_callable=AsyncMock)
    async def test_merge_strategy_keeps_non_null_fields(self, mock_find: AsyncMock, org_id: uuid.UUID) -> None:
        record = {
            "__table__": "accounts",
            "id": "u1",
            "data": {"id": "u1", "email": "new@x.com", "display_name": "Renamed"},
        }
        existing = _StubMigrateAccount(
            id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
            email="old@x.com",
            display_name=None,
        )
        mock_find.return_value = existing
        session = self._session()
        counts = await _import_org_data(session, org_id, [record], "merge")
        assert counts["overwritten"] == 1
        # Merge never clobbers a non-null existing value.
        assert existing.email == "old@x.com"
        # Null existing fields are backfilled.
        assert existing.display_name == "Renamed"

    @patch("modulo.cli.migrate._MODEL_MAP", {"accounts": _StubMigrateAccount})
    @patch("modulo.cli.migrate._find_existing_row", new_callable=AsyncMock)
    async def test_unknown_table_counts_error(self, mock_find: AsyncMock, org_id: uuid.UUID) -> None:
        session = self._session()
        counts = await _import_org_data(
            session, org_id, [{"__table__": "no_such_table", "id": "1", "data": {}}], "skip"
        )
        assert counts["errors"] == 1
        session.add.assert_not_called()

    @patch("modulo.cli.migrate._MODEL_MAP", {"accounts": _StubMigrateAccount})
    @patch("modulo.cli.migrate._find_existing_row", new_callable=AsyncMock)
    async def test_missing_data_key_counts_error(self, mock_find: AsyncMock, org_id: uuid.UUID) -> None:
        session = self._session()
        counts = await _import_org_data(session, org_id, [{"__table__": "accounts", "id": "1"}], "skip")
        assert counts["errors"] == 1
        session.add.assert_not_called()

    @patch("modulo.cli.migrate._MODEL_MAP", {"accounts": _StubMigrateAccount})
    async def test_mutually_exclusive_flags_raise(self, org_id: uuid.UUID) -> None:
        session = self._session()
        with pytest.raises(ClickException, match="mutually exclusive"):
            await _import_org_data(session, org_id, [dict(self._RECORD)], "skip", pipelines_only=True, users_only=True)


# ── Verify command tests ─────────────────────────────────────────────────────


class TestVerifyExportCmd:
    @patch("modulo.cli.migrate.decode_principal")
    @patch("modulo.cli.migrate._verify_export")
    def test_verify_success(self, mock_verify: MagicMock, mock_decode: MagicMock, tmp_path: Path) -> None:
        from modulo.auth.jwt import AuthenticatedPrincipal

        mock_decode.return_value = AuthenticatedPrincipal(
            username="admin",
            organisation_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            account_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
            org_role="admin",
        )
        mock_verify.return_value = True

        input_path = tmp_path / "verify.jsonl"
        input_path.write_text(json.dumps({"__meta__": {"version": 1, "export_hash": "abc"}}) + "\n")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--token",
                "fake.jwt.token",
                "verify-export",
                "00000000-0000-0000-0000-000000000001",
                "--input",
                str(input_path),
            ],
        )
        assert result.exit_code == 0

    @patch("modulo.cli.migrate.decode_principal")
    @patch("modulo.cli.migrate._verify_export")
    def test_verify_failure(self, mock_verify: MagicMock, mock_decode: MagicMock, tmp_path: Path) -> None:
        from modulo.auth.jwt import AuthenticatedPrincipal

        mock_decode.return_value = AuthenticatedPrincipal(
            username="admin",
            organisation_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            account_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
            org_role="admin",
        )
        mock_verify.return_value = False

        input_path = tmp_path / "verify_fail.jsonl"
        input_path.write_text(json.dumps({"__meta__": {"version": 1, "export_hash": "wrong"}}) + "\n")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--token",
                "fake.jwt.token",
                "verify-export",
                "00000000-0000-0000-0000-000000000001",
                "--input",
                str(input_path),
            ],
        )
        assert result.exit_code != 0
        assert "Verification failed" in result.output


# ── Options / flags tests ────────────────────────────────────────────────────


class TestFlags:
    @patch("modulo.cli.migrate.decode_principal")
    @patch("modulo.cli.migrate.AsyncSessionLocal")
    @patch("modulo.cli.migrate.get_organisation")
    @patch("modulo.cli.migrate.get_account_by_id", new_callable=AsyncMock)
    @patch("modulo.cli.migrate.get_membership_by_account_and_org")
    def test_export_pipelines_only(
        self,
        mock_get_membership: MagicMock,
        mock_get_account: AsyncMock,
        mock_get_org: MagicMock,
        mock_session_local: MagicMock,
        mock_decode: MagicMock,
        org_id: uuid.UUID,
        admin_user_id: uuid.UUID,
        tmp_path: Path,
    ) -> None:
        from modulo.auth.jwt import AuthenticatedPrincipal

        mock_decode.return_value = AuthenticatedPrincipal(
            username="admin",
            organisation_id=org_id,
            account_id=admin_user_id,
            org_role="admin",
        )
        mock_admin = MagicMock()
        mock_admin.org_role = "admin"
        mock_admin.organisation_id = org_id
        mock_get_account.return_value = mock_admin
        mock_get_membership.return_value = MagicMock(role="admin")

        mock_org = MockModel(id=org_id)
        mock_get_org.return_value = mock_org

        mock_session = AsyncMock()

        class FakeResult:
            def scalars(self):
                return self

            def all(self):
                return []

        async def fake_execute(_stmt):
            return FakeResult()

        mock_session.execute = fake_execute
        mock_session.__aenter__.return_value = mock_session
        mock_session_local.return_value.__aenter__.return_value = mock_session

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--token",
                "fake.jwt.token",
                "export-org",
                str(org_id),
                "--output",
                str(tmp_path / "p.jsonl"),
                "--pipelines-only",
            ],
        )
        assert result.exit_code == 0, f"CLI failed: {result.output}"

    @patch("modulo.cli.migrate.decode_principal")
    @patch("modulo.cli.migrate.AsyncSessionLocal")
    @patch("modulo.cli.migrate.get_organisation")
    @patch("modulo.cli.migrate.get_account_by_id", new_callable=AsyncMock)
    @patch("modulo.cli.migrate.get_membership_by_account_and_org")
    def test_import_users_only(
        self,
        mock_get_membership: MagicMock,
        mock_get_account: AsyncMock,
        mock_get_org: MagicMock,
        mock_session_local: MagicMock,
        mock_decode: MagicMock,
        org_id: uuid.UUID,
        admin_user_id: uuid.UUID,
        tmp_path: Path,
    ) -> None:
        from modulo.auth.jwt import AuthenticatedPrincipal

        mock_decode.return_value = AuthenticatedPrincipal(
            username="admin",
            organisation_id=org_id,
            account_id=admin_user_id,
            org_role="admin",
        )
        mock_admin = MagicMock()
        mock_admin.org_role = "admin"
        mock_admin.organisation_id = org_id
        mock_get_account.return_value = mock_admin
        mock_get_membership.return_value = MagicMock(role="admin")
        mock_get_org.return_value = MagicMock()

        mock_session = AsyncMock()
        mock_session.execute.return_value.scalar_one_or_none.return_value = None
        mock_session.__aenter__.return_value = mock_session
        mock_session_local.return_value.__aenter__.return_value = mock_session

        _empty_hash = hashlib.sha256().hexdigest()
        input_path = tmp_path / "import_users.jsonl"
        input_path.write_text(json.dumps({"__meta__": {"version": 1, "export_hash": _empty_hash}}) + "\n")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--token",
                "fake.jwt.token",
                "import-org",
                str(org_id),
                "--input",
                str(input_path),
                "--users-only",
            ],
        )
        assert result.exit_code == 0, f"CLI failed: {result.output}"


# ── MODULO_ADMIN_SECRET env var auth ─────────────────────────────────────────


# ── Import hash verification ──────────────────────────────────────────────────


class TestImportHashVerification:
    @patch("modulo.cli.migrate.decode_principal")
    @patch("modulo.cli.migrate.AsyncSessionLocal")
    @patch("modulo.cli.migrate.get_organisation")
    @patch("modulo.cli.migrate.get_account_by_id", new_callable=AsyncMock)
    @patch("modulo.cli.migrate.get_membership_by_account_and_org")
    def test_import_hash_mismatch_aborts(
        self,
        mock_get_membership: MagicMock,
        mock_get_account: AsyncMock,
        mock_get_org: MagicMock,
        mock_session_local: MagicMock,
        mock_decode: MagicMock,
        org_id: uuid.UUID,
        admin_user_id: uuid.UUID,
        tmp_path: Path,
    ) -> None:
        from modulo.auth.jwt import AuthenticatedPrincipal

        mock_decode.return_value = AuthenticatedPrincipal(
            username="admin",
            organisation_id=org_id,
            account_id=admin_user_id,
            org_role="admin",
        )
        mock_admin = MagicMock()
        mock_admin.org_role = "admin"
        mock_admin.organisation_id = org_id
        mock_get_account.return_value = mock_admin
        mock_get_membership.return_value = MagicMock(role="admin")
        mock_get_org.return_value = MagicMock()

        mock_session = AsyncMock()
        mock_session.execute.return_value.scalar_one_or_none.return_value = None
        mock_session.__aenter__.return_value = mock_session
        mock_session_local.return_value.__aenter__.return_value = mock_session

        input_path = tmp_path / "bad_hash.jsonl"
        input_path.write_text(json.dumps({"__meta__": {"version": 1, "export_hash": "definitely_wrong"}}) + "\n")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--token",
                "fake.jwt.token",
                "import-org",
                str(org_id),
                "--input",
                str(input_path),
            ],
        )
        assert result.exit_code != 0
        assert "hash verification failed" in result.output.lower() or "Import aborted" in result.output

    @patch("modulo.cli.migrate.decode_principal")
    @patch("modulo.cli.migrate.AsyncSessionLocal")
    @patch("modulo.cli.migrate.get_organisation")
    @patch("modulo.cli.migrate.get_account_by_id", new_callable=AsyncMock)
    @patch("modulo.cli.migrate.get_membership_by_account_and_org")
    def test_import_skips_verify_when_no_hash(
        self,
        mock_get_membership: MagicMock,
        mock_get_account: AsyncMock,
        mock_get_org: MagicMock,
        mock_session_local: MagicMock,
        mock_decode: MagicMock,
        org_id: uuid.UUID,
        admin_user_id: uuid.UUID,
        tmp_path: Path,
    ) -> None:
        from modulo.auth.jwt import AuthenticatedPrincipal

        mock_decode.return_value = AuthenticatedPrincipal(
            username="admin",
            organisation_id=org_id,
            account_id=admin_user_id,
            org_role="admin",
        )
        mock_admin = MagicMock()
        mock_admin.org_role = "admin"
        mock_admin.organisation_id = org_id
        mock_get_account.return_value = mock_admin
        mock_get_membership.return_value = MagicMock(role="admin")
        mock_get_org.return_value = MagicMock()

        mock_session = AsyncMock()
        mock_session.execute.return_value.scalar_one_or_none.return_value = None
        mock_session.__aenter__.return_value = mock_session
        mock_session_local.return_value.__aenter__.return_value = mock_session

        input_path = tmp_path / "no_hash.jsonl"
        input_path.write_text(json.dumps({"__meta__": {"version": 1}}) + "\n")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--token",
                "fake.jwt.token",
                "import-org",
                str(org_id),
                "--input",
                str(input_path),
            ],
        )
        assert result.exit_code == 0, f"CLI failed: {result.output}"


# ── Auth-first enforcement (auth before file read) ──────────────────────────


class TestAuthBeforeFileRead:
    @patch("modulo.cli.migrate.decode_principal")
    @patch("modulo.cli.migrate.AsyncSessionLocal")
    @patch("modulo.cli.migrate.get_account_by_id")
    def test_import_fails_on_auth_before_file_access(
        self,
        mock_get_account: MagicMock,
        mock_session_local: MagicMock,
        mock_decode: MagicMock,
        org_id: uuid.UUID,
        tmp_path: Path,
    ) -> None:
        from modulo.auth.jwt import AuthenticatedPrincipal

        mock_decode.return_value = AuthenticatedPrincipal(
            username="admin",
            organisation_id=org_id,
            account_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
            org_role="admin",
        )
        mock_get_account.return_value = None

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session_local.return_value.__aenter__.return_value = mock_session

        input_path = tmp_path / "should_not_exist.jsonl"
        input_path.write_text(json.dumps({"__meta__": {"version": 1}}) + "\n")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "--token",
                "fake.jwt.token",
                "import-org",
                str(org_id),
                "--input",
                str(input_path),
            ],
        )
        assert result.exit_code != 0
        assert "Admin account not found" in result.output
        assert input_path.exists(), "File should still exist (auth fail occurred before any file access)"


class TestAdminSecretAuth:
    @patch.dict("os.environ", {"MODULO_ADMIN_SECRET": "super_secret", "MODULO_ADMIN_TOKEN": ""})
    @patch("modulo.cli.migrate.AsyncSessionLocal")
    @patch("modulo.cli.migrate.get_organisation")
    @patch("modulo.cli.migrate.get_account_by_id")
    def test_auth_with_env_secret(
        self,
        mock_get_account: MagicMock,
        mock_get_org: MagicMock,
        mock_session_local: MagicMock,
        org_id: uuid.UUID,
        admin_user_id: uuid.UUID,
        tmp_path: Path,
    ) -> None:
        mock_admin = MagicMock()
        mock_admin.org_role = "admin"
        mock_admin.organisation_id = org_id
        mock_get_account.return_value = mock_admin

        mock_org = MockModel(id=org_id)
        mock_get_org.return_value = mock_org

        mock_session = AsyncMock()

        class FakeResult:
            def scalars(self):
                return self

            def all(self):
                return []

        async def fake_execute(_stmt):
            return FakeResult()

        mock_session.execute = fake_execute
        mock_session.__aenter__.return_value = mock_session
        mock_session_local.return_value.__aenter__.return_value = mock_session

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "export-org",
                str(org_id),
                "--output",
                str(tmp_path / "secret.jsonl"),
            ],
        )
        assert result.exit_code == 0, f"CLI failed: {result.output}"
