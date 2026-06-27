"""Tests for the modulo-migrate CLI tool."""

import asyncio
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from modulo.cli.migrate import (
    _compute_export_hash,
    _group_records,
    _hash_record,
    _read_jsonl,
    _serialise_row,
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
        rec = {"a": 1, "b": "x"}
        h1 = _hash_record(rec)
        h2 = _hash_record(rec)
        assert h1 == h2

    def test_hash_record_different_inputs(self) -> None:
        assert _hash_record({"a": 1}) != _hash_record({"a": 2})


class TestComputeExportHash:
    def test_empty_bundle(self) -> None:
        h = _compute_export_hash({})
        assert isinstance(h, str)
        assert len(h) == 64

    def test_with_rows(self) -> None:
        bundle = {"users": [{"id": "u1", "name": "alice"}, {"id": "u2", "name": "bob"}]}
        h = _compute_export_hash(bundle)
        assert isinstance(h, str)
        assert len(h) == 64


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
        assert len(groups) == 0

    def test_empty_records(self) -> None:
        assert _group_records([]) == {}


class TestReadJsonl:
    def test_reads_meta_and_records(self, tmp_path: Path) -> None:
        path = tmp_path / "export.jsonl"
        path.write_text(
            json.dumps({"__meta__": {"version": 1, "export_hash": "abc"}})
            + "\n"
            + json.dumps({"__table__": "users", "id": "1", "data": {"name": "a"}})
            + "\n"
            + json.dumps({"__table__": "users", "id": "2", "data": {"name": "b"}})
            + "\n"
        )
        import asyncio

        meta, records = asyncio.run(_read_jsonl(path))
        assert meta["version"] == 1
        assert meta["export_hash"] == "abc"
        assert len(records) == 2

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        import asyncio

        meta, records = asyncio.run(_read_jsonl(path))
        assert meta == {}
        assert records == []


class TestWriteJsonl:
    def test_writes_header_and_records(self, tmp_path: Path) -> None:
        path = tmp_path / "out.jsonl"
        bundle = {
            "users": [{"id": "u1", "name": "alice"}],
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
        result = asyncio.run(_verify_export(meta, records))
        assert result is True

    def test_verify_mismatch(self) -> None:
        meta = {"export_hash": "aaa"}
        records = [{"__table__": "users", "id": "1", "__hash__": _hash_record({"id": "1"})}]
        result = asyncio.run(_verify_export(meta, records))
        assert result is False


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
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
            org_role="admin",
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["--token", "fake.jwt.token", "export-org", "00000000-0000-0000-0000-000000000001"])
        # Will fail later due to DB, but auth should pass
        assert "Admin authentication required" not in result.output


# ── Export command tests ─────────────────────────────────────────────────────


class TestExportOrg:
    @patch("modulo.cli.migrate.decode_principal")
    @patch("modulo.cli.migrate.AsyncSessionLocal")
    @patch("modulo.cli.migrate.get_organisation")
    @patch("modulo.cli.migrate.get_user_by_id")
    def test_export_basic(
        self,
        mock_get_user: MagicMock,
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
            user_id=admin_user_id,
            org_role="admin",
        )
        mock_admin = MagicMock()
        mock_admin.org_role = "admin"
        mock_admin.organisation_id = org_id
        mock_get_user.return_value = mock_admin

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
    @patch("modulo.cli.migrate.get_user_by_id")
    def test_export_org_not_found(
        self,
        mock_get_user: MagicMock,
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
            user_id=admin_user_id,
            org_role="admin",
        )
        mock_admin = MagicMock()
        mock_admin.org_role = "admin"
        mock_admin.organisation_id = org_id
        mock_get_user.return_value = mock_admin
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


# ── Import command tests ─────────────────────────────────────────────────────


class TestImportOrg:
    @patch("modulo.cli.migrate.decode_principal")
    @patch("modulo.cli.migrate.AsyncSessionLocal")
    @patch("modulo.cli.migrate.get_organisation")
    @patch("modulo.cli.migrate.get_user_by_id")
    def test_import_basic(
        self,
        mock_get_user: MagicMock,
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
            user_id=admin_user_id,
            org_role="admin",
        )
        mock_admin = MagicMock()
        mock_admin.org_role = "admin"
        mock_admin.organisation_id = org_id
        mock_get_user.return_value = mock_admin
        mock_get_org.return_value = MagicMock()

        mock_session = AsyncMock()
        mock_session.execute.return_value.scalar_one_or_none.return_value = None
        mock_session.__aenter__.return_value = mock_session
        mock_session_local.return_value.__aenter__.return_value = mock_session

        input_path = tmp_path / "import.jsonl"
        input_path.write_text(json.dumps({"__meta__": {"version": 1, "export_hash": "abc"}}) + "\n")

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
    @patch("modulo.cli.migrate.get_user_by_id")
    def test_import_with_conflict_strategies(
        self,
        mock_get_user: MagicMock,
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
            user_id=admin_user_id,
            org_role="admin",
        )
        mock_admin = MagicMock()
        mock_admin.org_role = "admin"
        mock_admin.organisation_id = org_id
        mock_get_user.return_value = mock_admin
        mock_get_org.return_value = MagicMock()

        mock_session = AsyncMock()
        mock_session.execute.return_value.scalar_one_or_none.return_value = None
        mock_session.__aenter__.return_value = mock_session
        mock_session_local.return_value.__aenter__.return_value = mock_session

        input_path = tmp_path / "import_skip.jsonl"
        input_path.write_text(json.dumps({"__meta__": {"version": 1, "export_hash": "abc"}}) + "\n")

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


# ── Verify command tests ─────────────────────────────────────────────────────


class TestVerifyExportCmd:
    @patch("modulo.cli.migrate.decode_principal")
    @patch("modulo.cli.migrate._verify_export")
    def test_verify_success(self, mock_verify: MagicMock, mock_decode: MagicMock, tmp_path: Path) -> None:
        from modulo.auth.jwt import AuthenticatedPrincipal

        mock_decode.return_value = AuthenticatedPrincipal(
            username="admin",
            organisation_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
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
            user_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
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
    @patch("modulo.cli.migrate.get_user_by_id")
    def test_export_pipelines_only(
        self,
        mock_get_user: MagicMock,
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
            user_id=admin_user_id,
            org_role="admin",
        )
        mock_admin = MagicMock()
        mock_admin.org_role = "admin"
        mock_admin.organisation_id = org_id
        mock_get_user.return_value = mock_admin

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
    @patch("modulo.cli.migrate.get_user_by_id")
    def test_import_users_only(
        self,
        mock_get_user: MagicMock,
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
            user_id=admin_user_id,
            org_role="admin",
        )
        mock_admin = MagicMock()
        mock_admin.org_role = "admin"
        mock_admin.organisation_id = org_id
        mock_get_user.return_value = mock_admin
        mock_get_org.return_value = MagicMock()

        mock_session = AsyncMock()
        mock_session.execute.return_value.scalar_one_or_none.return_value = None
        mock_session.__aenter__.return_value = mock_session
        mock_session_local.return_value.__aenter__.return_value = mock_session

        input_path = tmp_path / "import_users.jsonl"
        input_path.write_text(json.dumps({"__meta__": {"version": 1, "export_hash": "abc"}}) + "\n")

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


class TestAdminSecretAuth:
    @patch.dict("os.environ", {"MODULO_ADMIN_SECRET": "super_secret", "MODULO_ADMIN_TOKEN": ""})
    @patch("modulo.cli.migrate.AsyncSessionLocal")
    @patch("modulo.cli.migrate.get_organisation")
    @patch("modulo.cli.migrate.get_user_by_id")
    def test_auth_with_env_secret(
        self,
        mock_get_user: MagicMock,
        mock_get_org: MagicMock,
        mock_session_local: MagicMock,
        org_id: uuid.UUID,
        admin_user_id: uuid.UUID,
        tmp_path: Path,
    ) -> None:
        mock_admin = MagicMock()
        mock_admin.org_role = "admin"
        mock_admin.organisation_id = org_id
        mock_get_user.return_value = mock_admin

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
