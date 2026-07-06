"""Tests for the modulo backup/restore CLI tool."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from modulo.cli.backup import (
    _export_checkpoint_blobs_sync,
    _export_credentials_references_sync,
    _fernet_key_hash,
    _human_size,
    _re_encrypt_credentials_sync,
    _resolve_url,
    _restore_checkpoint_blobs_sync,
    _run_pg_dump,
    _run_psql,
    _serialise_for_json,
    _write_json,
    cli,
)

# ── _resolve_url ──────────────────────────────────────────────────────────────


class TestResolveUrl:
    def test_strips_asyncpg_prefix(self) -> None:
        url = "postgresql+asyncpg://user:pass@localhost:5432/db"
        assert _resolve_url(url) == "postgresql://user:pass@localhost:5432/db"

    def test_strips_psycopg_prefix(self) -> None:
        url = "postgresql+psycopg://user:pass@localhost:5432/db"
        assert _resolve_url(url) == "postgresql://user:pass@localhost:5432/db"

    def test_plain_postgresql_passthrough(self) -> None:
        url = "postgresql://user:pass@localhost:5432/db"
        assert _resolve_url(url) == url

    def test_unknown_scheme_passthrough(self) -> None:
        url = "sqlite:///local.db"
        assert _resolve_url(url) == url

    @patch("modulo.cli.backup.get_settings")
    def test_none_fallback(self, mock_settings: MagicMock) -> None:
        mock_settings.return_value.database_url = "postgresql+asyncpg://fallback/db"
        result = _resolve_url(None)
        assert result == "postgresql://fallback/db"


# ── _fernet_key_hash ──────────────────────────────────────────────────────────


class TestFernetKeyHash:
    def test_deterministic(self) -> None:
        h1 = _fernet_key_hash("test-key-1234")
        h2 = _fernet_key_hash("test-key-1234")
        assert h1 == h2

    def test_length(self) -> None:
        h = _fernet_key_hash("any-key")
        assert len(h) == 16

    def test_different_keys_produce_different_hashes(self) -> None:
        assert _fernet_key_hash("key-a") != _fernet_key_hash("key-b")

    def test_empty_key(self) -> None:
        h = _fernet_key_hash("")
        assert isinstance(h, str)
        assert len(h) == 16


# ── _serialise_for_json ──────────────────────────────────────────────────────


class TestSerialiseForJson:
    def test_uuid_to_str(self) -> None:
        uid = uuid.uuid4()
        result = _serialise_for_json(uid)
        assert result == str(uid)
        assert isinstance(result, str)

    def test_bytes_to_hex(self) -> None:
        result = _serialise_for_json(b"\x00\xff\xab")
        assert result == "00ffab"

    def test_bytes_empty(self) -> None:
        result = _serialise_for_json(b"")
        assert result == ""

    def test_datetime_to_isoformat(self) -> None:
        dt = datetime.now(UTC)
        result = _serialise_for_json(dt)
        assert result == dt.isoformat()

    def test_int_passthrough(self) -> None:
        assert _serialise_for_json(42) == 42

    def test_float_passthrough(self) -> None:
        assert _serialise_for_json(3.14) == 3.14

    def test_str_passthrough(self) -> None:
        assert _serialise_for_json("hello") == "hello"

    def test_list_passthrough(self) -> None:
        val = [1, 2, 3]
        assert _serialise_for_json(val) is val

    def test_dict_passthrough(self) -> None:
        val = {"a": 1}
        assert _serialise_for_json(val) is val

    def test_none_passthrough(self) -> None:
        assert _serialise_for_json(None) is None


# ── _human_size ──────────────────────────────────────────────────────────────


class TestHumanSize:
    def test_zero_bytes(self) -> None:
        assert _human_size(0) == "0.0 B"

    def test_bytes(self) -> None:
        assert _human_size(512) == "512.0 B"
        assert _human_size(1023) == "1023.0 B"

    def test_kilobytes(self) -> None:
        assert _human_size(1024) == "1.0 KB"
        assert _human_size(2048) == "2.0 KB"
        assert _human_size(1536) == "1.5 KB"

    def test_megabytes(self) -> None:
        assert _human_size(1024 * 1024) == "1.0 MB"
        assert _human_size(2.5 * 1024 * 1024) == "2.5 MB"

    def test_gigabytes(self) -> None:
        assert _human_size(1024**3) == "1.0 GB"

    def test_terabytes(self) -> None:
        assert _human_size(1024**4) == "1.0 TB"
        assert _human_size(2 * 1024**4) == "2.0 TB"


# ── _write_json ───────────────────────────────────────────────────────────────


class TestWriteJson:
    def test_writes_valid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "test.json"
        _write_json(path, {"key": "value", "num": 42})
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == {"key": "value", "num": 42}

    def test_writes_indented(self, tmp_path: Path) -> None:
        path = tmp_path / "pretty.json"
        _write_json(path, {"a": 1})
        text = path.read_text(encoding="utf-8")
        assert '"a": 1' in text

    def test_serialises_uuid_via_default(self, tmp_path: Path) -> None:
        uid = uuid.uuid4()
        path = tmp_path / "uuid.json"
        _write_json(path, {"id": uid})
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["id"] == str(uid)

    def test_serialises_bytes_via_default(self, tmp_path: Path) -> None:
        path = tmp_path / "bytes.json"
        _write_json(path, {"blob": b"\x00\xff"})
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["blob"] == "00ff"

    def test_serialises_datetime_via_default(self, tmp_path: Path) -> None:
        dt = datetime.now(UTC)
        path = tmp_path / "dt.json"
        _write_json(path, {"ts": dt})
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["ts"] == dt.isoformat()


# ── _run_pg_dump ──────────────────────────────────────────────────────────────


class TestRunPgDump:
    @patch("modulo.cli.backup.subprocess.run")
    def test_success(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_run.return_value = mock_process

        output = tmp_path / "dump.sql"
        _run_pg_dump("postgresql://user:pass@localhost/db", output)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "pg_dump"
        assert "--clean" in cmd
        assert "--if-exists" in cmd
        assert "--no-owner" in cmd
        assert "--no-acl" in cmd
        assert "--format=plain" in cmd
        assert output.name in str(mock_run.call_args[1]["stdout"])
        assert mock_run.call_args[1]["timeout"] == 300

    @patch("modulo.cli.backup.subprocess.run")
    def test_failure_raises_runtime_error(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.stderr.decode.return_value = "connection to server failed"
        mock_run.return_value = mock_process

        output = tmp_path / "dump.sql"
        with pytest.raises(RuntimeError, match="pg_dump failed: connection to server failed"):
            _run_pg_dump("postgresql://user:pass@localhost/db", output)

    @patch("modulo.cli.backup.subprocess.run")
    def test_custom_timeout(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_run.return_value = mock_process

        output = tmp_path / "dump.sql"
        _run_pg_dump("postgresql://user:pass@localhost/db", output, timeout=600)

        assert mock_run.call_args[1]["timeout"] == 600


# ── _run_psql ─────────────────────────────────────────────────────────────────


class TestRunPsql:
    @patch("modulo.cli.backup.subprocess.run")
    def test_success(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_run.return_value = mock_process

        input_path = tmp_path / "restore.sql"
        input_path.write_text("RESTORE;")

        _run_psql("postgresql://user:pass@localhost/db", input_path)

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "psql"
        assert "-q" in cmd
        assert "-v" in cmd
        assert "ON_ERROR_STOP=1" in cmd
        assert mock_run.call_args[1]["timeout"] == 600

    @patch("modulo.cli.backup.subprocess.run")
    def test_failure_raises_runtime_error(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.stderr.decode.return_value = "syntax error at line 1"
        mock_run.return_value = mock_process

        input_path = tmp_path / "fail.sql"
        input_path.write_text("BAD SQL;")

        with pytest.raises(RuntimeError, match="psql restore failed: syntax error at line 1"):
            _run_psql("postgresql://user:pass@localhost/db", input_path)

    @patch("modulo.cli.backup.subprocess.run")
    def test_custom_timeout(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_run.return_value = mock_process

        input_path = tmp_path / "restore.sql"
        input_path.write_text("RESTORE;")

        _run_psql("postgresql://user:pass@localhost/db", input_path, timeout=1200)

        assert mock_run.call_args[1]["timeout"] == 1200


# ── _export_checkpoint_blobs_sync ─────────────────────────────────────────────


class TestExportCheckpointBlobsSync:
    @patch("psycopg.connect")
    def test_returns_formatted_rows(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        row1 = {
            "organisation_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
            "thread_id": "thread-1",
            "checkpoint_ns": "ns1",
            "channel": "default",
            "version": 1,
            "type": "json",
            "blob": b"\x00\xff",
        }
        row2 = {
            "organisation_id": uuid.UUID("00000000-0000-0000-0000-000000000002"),
            "thread_id": "thread-2",
            "checkpoint_ns": "ns2",
            "channel": "other",
            "version": 2,
            "type": "json",
            "blob": None,
        }
        mock_cur.__iter__.return_value = [row1, row2]

        result = _export_checkpoint_blobs_sync("postgresql://localhost/db")

        assert len(result) == 2
        assert result[0]["organisation_id"] == "00000000-0000-0000-0000-000000000001"
        assert result[0]["thread_id"] == "thread-1"
        assert result[0]["blob"] == "00ff"
        assert result[1]["organisation_id"] == "00000000-0000-0000-0000-000000000002"
        assert result[1]["blob"] is None

        mock_connect.assert_called_once()
        mock_cur.execute.assert_called_once()
        sql = mock_cur.execute.call_args[0][0]
        assert "checkpoint_blobs" in sql
        assert "ORDER BY" in sql

    @patch("psycopg.connect")
    def test_handles_memoryview_blob(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        row = {
            "organisation_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
            "thread_id": "t1",
            "checkpoint_ns": "ns",
            "channel": "ch",
            "version": 1,
            "type": "json",
            "blob": memoryview(b"\xab\xcd"),
        }
        mock_cur.__iter__.return_value = [row]

        result = _export_checkpoint_blobs_sync("postgresql://localhost/db")

        assert result[0]["blob"] == "abcd"


# ── _export_credentials_references_sync ───────────────────────────────────────


class TestExportCredentialsReferencesSync:
    @patch("psycopg.connect")
    def test_returns_dict_with_two_tables(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__.return_value = mock_conn

        cur1 = MagicMock()
        cur1.__enter__.return_value = cur1
        cur1.__iter__.return_value = [
            {
                "id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
                "organisation_id": uuid.UUID("00000000-0000-0000-0000-000000000010"),
                "name": "My Connector",
                "credentials_ciphertext": b"\xca\xfe",
            },
        ]

        cur2 = MagicMock()
        cur2.__enter__.return_value = cur2
        cur2.__iter__.return_value = [
            {
                "id": uuid.UUID("00000000-0000-0000-0000-000000000002"),
                "organisation_id": uuid.UUID("00000000-0000-0000-0000-000000000010"),
                "name": "My Model",
                "credentials_ciphertext": b"\xba\xbe",
            },
        ]

        mock_conn.cursor.side_effect = [cur1, cur2]

        result = _export_credentials_references_sync("postgresql://localhost/db")

        assert set(result.keys()) == {"connector_instances", "model_backends"}
        assert len(result["connector_instances"]) == 1
        assert result["connector_instances"][0]["id"] == "00000000-0000-0000-0000-000000000001"
        assert result["connector_instances"][0]["credentials_ciphertext"] == "cafe"
        assert len(result["model_backends"]) == 1
        assert result["model_backends"][0]["credentials_ciphertext"] == "babe"

    @patch("psycopg.connect")
    def test_returns_empty_dicts_for_empty_tables(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__.return_value = mock_conn

        cur1 = MagicMock()
        cur1.__enter__.return_value = cur1
        cur1.__iter__.return_value = []

        cur2 = MagicMock()
        cur2.__enter__.return_value = cur2
        cur2.__iter__.return_value = []

        mock_conn.cursor.side_effect = [cur1, cur2]

        result = _export_credentials_references_sync("postgresql://localhost/db")

        assert result == {"connector_instances": [], "model_backends": []}

    @patch("psycopg.connect")
    def test_executes_correct_sql_for_each_table(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value.__enter__.return_value = mock_conn

        cur1 = MagicMock()
        cur1.__enter__.return_value = cur1
        cur1.__iter__.return_value = []

        cur2 = MagicMock()
        cur2.__enter__.return_value = cur2
        cur2.__iter__.return_value = []

        mock_conn.cursor.side_effect = [cur1, cur2]

        _export_credentials_references_sync("postgresql://localhost/db")

        sql1 = cur1.execute.call_args[0][0]
        assert "connector_instances" in sql1
        sql2 = cur2.execute.call_args[0][0]
        assert "model_backends" in sql2


# ── _restore_checkpoint_blobs_sync ────────────────────────────────────────────


class TestRestoreCheckpointBlobsSync:
    @patch("psycopg.connect")
    def test_truncates_and_inserts(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        blobs = [
            {
                "organisation_id": "00000000-0000-0000-0000-000000000001",
                "thread_id": "t1",
                "checkpoint_ns": "ns",
                "channel": "ch",
                "version": 1,
                "type": "json",
                "blob": "00ff",
            },
        ]

        result = _restore_checkpoint_blobs_sync("postgresql://localhost/db", blobs)

        assert result == 1
        mock_cur.execute.assert_any_call("TRUNCATE TABLE checkpoint_blobs")
        insert_call = mock_cur.execute.call_args_list[1]
        assert "INSERT INTO checkpoint_blobs" in insert_call[0][0]
        mock_conn.commit.assert_called_once()

    @patch("psycopg.connect")
    def test_handles_null_blob(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        blobs = [
            {
                "organisation_id": "00000000-0000-0000-0000-000000000001",
                "thread_id": "t1",
                "checkpoint_ns": "ns",
                "channel": "ch",
                "version": 1,
                "type": "json",
                "blob": None,
            },
        ]

        result = _restore_checkpoint_blobs_sync("postgresql://localhost/db", blobs)

        assert result == 1
        insert_call = mock_cur.execute.call_args_list[1]
        params = insert_call[0][1]
        assert params[-1] is None

    @patch("psycopg.connect")
    def test_returns_count(self, mock_connect: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        blobs = [
            {
                "organisation_id": "00000000-0000-0000-0000-000000000001",
                "thread_id": "t1",
                "checkpoint_ns": "ns",
                "channel": "ch",
                "version": 1,
                "type": "json",
                "blob": "00",
            },
            {
                "organisation_id": "00000000-0000-0000-0000-000000000002",
                "thread_id": "t2",
                "checkpoint_ns": "ns",
                "channel": "ch",
                "version": 2,
                "type": "json",
                "blob": "ff",
            },
            {
                "organisation_id": "00000000-0000-0000-0000-000000000003",
                "thread_id": "t3",
                "checkpoint_ns": "ns",
                "channel": "ch",
                "version": 3,
                "type": "json",
                "blob": None,
            },
        ]

        result = _restore_checkpoint_blobs_sync("postgresql://localhost/db", blobs)

        assert result == 3
        assert mock_cur.execute.call_count == 4  # 1 TRUNCATE + 3 INSERT


# ── _re_encrypt_credentials_sync ──────────────────────────────────────────────


class TestReEncryptCredentialsSync:
    @patch("modulo.cli.backup.Fernet")
    @patch("psycopg.connect")
    def test_re_encrypts_rows(self, mock_connect: MagicMock, mock_fernet_cls: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        mock_old = MagicMock()
        mock_new = MagicMock()
        mock_old.decrypt.return_value = b"plaintext_data"
        mock_new.encrypt.return_value = b"new_ciphertext"
        mock_fernet_cls.side_effect = [mock_old, mock_new]

        creds = {
            "connector_instances": [
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "credentials_ciphertext": "cafe",
                },
            ],
        }

        result = _re_encrypt_credentials_sync(
            "postgresql://localhost/db", creds, "old-key-1234567890", "new-key-1234567890"
        )

        assert result == {"connector_instances": 1}
        mock_old.decrypt.assert_called_once()
        mock_new.encrypt.assert_called_once_with(b"plaintext_data")
        mock_conn.commit.assert_called_once()
        update_call = mock_cur.execute.call_args[0]
        assert "UPDATE connector_instances" in update_call[0]
        assert update_call[1][0] == b"new_ciphertext"

    @patch("modulo.cli.backup.Fernet")
    @patch("psycopg.connect")
    def test_skips_rows_with_empty_ciphertext(self, mock_connect: MagicMock, mock_fernet_cls: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        mock_old = MagicMock()
        mock_new = MagicMock()
        mock_fernet_cls.side_effect = [mock_old, mock_new]

        creds = {
            "connector_instances": [
                {"id": "00000000-0000-0000-0000-000000000001", "credentials_ciphertext": ""},
                {"id": "00000000-0000-0000-0000-000000000002", "credentials_ciphertext": "cafe"},
            ],
        }

        result = _re_encrypt_credentials_sync("postgresql://localhost/db", creds, "old-key", "new-key")

        assert result == {"connector_instances": 1}
        assert mock_old.decrypt.call_count == 1
        assert mock_new.encrypt.call_count == 1

    @patch("modulo.cli.backup.Fernet")
    @patch("psycopg.connect")
    def test_processes_multiple_tables(self, mock_connect: MagicMock, mock_fernet_cls: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        mock_old = MagicMock()
        mock_new = MagicMock()
        mock_old.decrypt.return_value = b"data"
        mock_new.encrypt.return_value = b"ct"
        mock_fernet_cls.side_effect = [mock_old, mock_new]

        creds = {
            "connector_instances": [
                {"id": "00000000-0000-0000-0000-000000000001", "credentials_ciphertext": "aa"},
            ],
            "model_backends": [
                {"id": "00000000-0000-0000-0000-000000000002", "credentials_ciphertext": "bb"},
            ],
        }

        result = _re_encrypt_credentials_sync("postgresql://localhost/db", creds, "old", "new")

        assert result == {"connector_instances": 1, "model_backends": 1}
        assert mock_cur.execute.call_count == 2  # 2 UPDATEs (one per table)
        mock_conn.commit.assert_called_once()

    @patch("modulo.cli.backup.Fernet")
    @patch("psycopg.connect")
    def test_handles_empty_creds_dict(self, mock_connect: MagicMock, mock_fernet_cls: MagicMock) -> None:
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur
        mock_connect.return_value.__enter__.return_value = mock_conn

        mock_old = MagicMock()
        mock_new = MagicMock()
        mock_fernet_cls.side_effect = [mock_old, mock_new]

        result = _re_encrypt_credentials_sync("postgresql://localhost/db", {}, "old", "new")

        assert result == {}
        mock_conn.commit.assert_called_once()


# ── CLI: backup ───────────────────────────────────────────────────────────────


class TestBackupCli:
    @patch("modulo.cli.backup._print_size")
    @patch("modulo.cli.backup._export_credentials_references_sync")
    @patch("modulo.cli.backup._export_checkpoint_blobs_sync")
    @patch("modulo.cli.backup._run_pg_dump")
    @patch("modulo.cli.backup._get_schema_versions")
    @patch("modulo.cli.backup._get_db_version")
    @patch("modulo.cli.backup.get_settings")
    def test_backup_success(
        self,
        mock_settings: MagicMock,
        mock_get_db_version: MagicMock,
        mock_get_schema_versions: MagicMock,
        mock_pg_dump: MagicMock,
        mock_export_blobs: MagicMock,
        mock_export_creds: MagicMock,
        mock_print_size: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_settings.return_value.fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_get_db_version.return_value = "PostgreSQL 16.0 (Debian 16.0-1)"
        mock_get_schema_versions.return_value = ["abc123def456"]
        mock_export_blobs.return_value = []
        mock_export_creds.return_value = {"connector_instances": [], "model_backends": []}

        runner = CliRunner()
        backup_dir = tmp_path / "mybackup"
        result = runner.invoke(cli, ["backup", "--output-dir", str(backup_dir)])

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "Backup complete" in result.output
        assert "Backup directory" in result.output
        assert "Running pg_dump" in result.output
        assert "database.sql written" in result.output

        mock_pg_dump.assert_called_once()
        mock_export_blobs.assert_called_once()
        mock_export_creds.assert_called_once()
        mock_print_size.assert_called_once()

    @patch("modulo.cli.backup._print_size")
    @patch("modulo.cli.backup._export_credentials_references_sync")
    @patch("modulo.cli.backup._export_checkpoint_blobs_sync")
    @patch("modulo.cli.backup._run_pg_dump")
    @patch("modulo.cli.backup._get_schema_versions")
    @patch("modulo.cli.backup._get_db_version")
    @patch("modulo.cli.backup.get_settings")
    def test_backup_writes_manifest(
        self,
        mock_settings: MagicMock,
        mock_get_db_version: MagicMock,
        mock_get_schema_versions: MagicMock,
        mock_pg_dump: MagicMock,
        mock_export_blobs: MagicMock,
        mock_export_creds: MagicMock,
        mock_print_size: MagicMock,
        tmp_path: Path,
    ) -> None:
        fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.fernet_key = fernet_key
        mock_get_db_version.return_value = "PostgreSQL 16.0"
        mock_get_schema_versions.return_value = ["rev123"]
        mock_export_blobs.return_value = []
        mock_export_creds.return_value = {"connector_instances": [], "model_backends": []}

        runner = CliRunner()
        backup_dir = tmp_path / "backup"
        runner.invoke(cli, ["backup", "--output-dir", str(backup_dir)])

        manifest_path = backup_dir / "backup-info.json"
        assert manifest_path.exists(), "backup-info.json not written"

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["backup_type"] == "full"
        assert manifest["db_version"] == "PostgreSQL 16.0"
        assert manifest["schema_versions"] == ["rev123"]
        assert manifest["fernet_key_hash"] == _fernet_key_hash(fernet_key)
        assert "timestamp" in manifest

    @patch("modulo.cli.backup.get_settings")
    def test_backup_failure_raises_click_exception(self, mock_settings: MagicMock, tmp_path: Path) -> None:
        mock_settings.return_value.fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.database_url = "postgresql://localhost/db"

        runner = CliRunner()
        backup_dir = tmp_path / "failbackup"
        result = runner.invoke(cli, ["backup", "--output-dir", str(backup_dir)])

        assert result.exit_code != 0
        assert isinstance(result.exception, SystemExit)

    @patch("modulo.cli.backup._print_size")
    @patch("modulo.cli.backup._export_credentials_references_sync")
    @patch("modulo.cli.backup._export_checkpoint_blobs_sync")
    @patch("modulo.cli.backup._run_pg_dump")
    @patch("modulo.cli.backup._get_schema_versions")
    @patch("modulo.cli.backup._get_db_version")
    @patch("modulo.cli.backup.get_settings")
    def test_backup_writes_blobs_and_creds_json(
        self,
        mock_settings: MagicMock,
        mock_get_db_version: MagicMock,
        mock_get_schema_versions: MagicMock,
        mock_pg_dump: MagicMock,
        mock_export_blobs: MagicMock,
        mock_export_creds: MagicMock,
        mock_print_size: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_settings.return_value.fernet_key = "a" * 32
        mock_get_db_version.return_value = "PostgreSQL 16.0"
        mock_get_schema_versions.return_value = ["rev1"]

        sample_blobs = [
            {
                "organisation_id": "00000000-0000-0000-0000-000000000001",
                "thread_id": "t1",
                "checkpoint_ns": "ns",
                "channel": "ch",
                "version": 1,
                "type": "json",
                "blob": "abcd",
            },
        ]
        sample_creds = {
            "connector_instances": [
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "organisation_id": "00000000-0000-0000-0000-000000000010",
                    "name": "Test Connector",
                    "credentials_ciphertext": "cafe",
                },
            ],
            "model_backends": [],
        }
        mock_export_blobs.return_value = sample_blobs
        mock_export_creds.return_value = sample_creds

        runner = CliRunner()
        backup_dir = tmp_path / "backup"
        runner.invoke(cli, ["backup", "--output-dir", str(backup_dir)])

        blobs_path = backup_dir / "checkpoint_blobs.json"
        assert blobs_path.exists()
        loaded_blobs = json.loads(blobs_path.read_text(encoding="utf-8"))
        assert len(loaded_blobs) == 1
        assert loaded_blobs[0]["blob"] == "abcd"

        creds_path = backup_dir / "credentials_references.json"
        assert creds_path.exists()
        loaded_creds = json.loads(creds_path.read_text(encoding="utf-8"))
        assert len(loaded_creds["connector_instances"]) == 1

    @patch("modulo.cli.backup._print_size")
    @patch("modulo.cli.backup._export_credentials_references_sync")
    @patch("modulo.cli.backup._export_checkpoint_blobs_sync")
    @patch("modulo.cli.backup._run_pg_dump")
    @patch("modulo.cli.backup._get_schema_versions")
    @patch("modulo.cli.backup._get_db_version")
    @patch("modulo.cli.backup.get_settings")
    def test_backup_with_explicit_db_url(
        self,
        mock_settings: MagicMock,
        mock_get_db_version: MagicMock,
        mock_get_schema_versions: MagicMock,
        mock_pg_dump: MagicMock,
        mock_export_blobs: MagicMock,
        mock_export_creds: MagicMock,
        mock_print_size: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_settings.return_value.fernet_key = "a" * 32
        mock_get_db_version.return_value = "PostgreSQL 16.0"
        mock_get_schema_versions.return_value = ["rev1"]
        mock_export_blobs.return_value = []
        mock_export_creds.return_value = {"connector_instances": [], "model_backends": []}

        runner = CliRunner()
        backup_dir = tmp_path / "backup"
        result = runner.invoke(
            cli,
            [
                "backup",
                "--output-dir",
                str(backup_dir),
                "--db-url",
                "postgresql://custom:pass@host/db",
            ],
        )

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        mock_pg_dump.assert_called_once()
        url_arg = mock_pg_dump.call_args[0][0]
        assert "custom" in url_arg
        assert "host" in url_arg


# ── CLI: restore ──────────────────────────────────────────────────────────────


class TestRestoreCli:
    @patch("modulo.cli.backup._re_encrypt_credentials_sync")
    @patch("modulo.cli.backup._restore_checkpoint_blobs_sync")
    @patch("modulo.cli.backup._run_psql")
    @patch("modulo.cli.backup.get_settings")
    def test_restore_success(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        tmp_path: Path,
    ) -> None:
        fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.fernet_key = fernet_key
        mock_restore_blobs.return_value = 5
        mock_re_encrypt.return_value = {}

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": ["rev1"],
            "fernet_key_hash": _fernet_key_hash(fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "database.sql").write_text("-- SQL dump", encoding="utf-8")
        (tmp_path / "checkpoint_blobs.json").write_text("[]", encoding="utf-8")
        (tmp_path / "credentials_references.json").write_text(
            '{"connector_instances": [], "model_backends": []}', encoding="utf-8"
        )

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--yes"])

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "Restore complete" in result.output
        assert "Restoring database schema" in result.output
        assert "restored" in result.output

        mock_psql.assert_called_once()
        mock_restore_blobs.assert_called_once()
        mock_re_encrypt.assert_not_called()

    def test_restore_missing_manifest(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--yes"])

        assert result.exit_code != 0
        assert "backup-info.json not found" in result.output

    @patch("modulo.cli.backup._re_encrypt_credentials_sync")
    @patch("modulo.cli.backup._restore_checkpoint_blobs_sync")
    @patch("modulo.cli.backup._run_psql")
    @patch("modulo.cli.backup.get_settings")
    def test_restore_skips_database_sql_when_missing(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        tmp_path: Path,
    ) -> None:
        fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.fernet_key = fernet_key
        mock_restore_blobs.return_value = 0
        mock_re_encrypt.return_value = {}

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": [],
            "fernet_key_hash": _fernet_key_hash(fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--yes"])

        assert result.exit_code == 0
        assert "No database.sql found" in result.output
        mock_psql.assert_not_called()

    @patch("modulo.cli.backup._re_encrypt_credentials_sync")
    @patch("modulo.cli.backup._restore_checkpoint_blobs_sync")
    @patch("modulo.cli.backup._run_psql")
    @patch("modulo.cli.backup.get_settings")
    def test_restore_requires_previous_fernet_key_when_key_changed(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        tmp_path: Path,
    ) -> None:
        new_fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        old_fernet_key = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        mock_settings.return_value.fernet_key = new_fernet_key
        mock_restore_blobs.return_value = 0

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": [],
            "fernet_key_hash": _fernet_key_hash(old_fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "database.sql").write_text("-- SQL", encoding="utf-8")
        (tmp_path / "credentials_references.json").write_text('{"connector_instances": []}', encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--yes"])

        assert result.exit_code != 0
        assert "--previous-fernet-key" in result.output

    @patch("modulo.cli.backup._re_encrypt_credentials_sync")
    @patch("modulo.cli.backup._restore_checkpoint_blobs_sync")
    @patch("modulo.cli.backup._run_psql")
    @patch("modulo.cli.backup.get_settings")
    def test_restore_with_re_encryption(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        tmp_path: Path,
    ) -> None:
        new_fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        old_fernet_key = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        mock_settings.return_value.fernet_key = new_fernet_key
        mock_restore_blobs.return_value = 0
        mock_re_encrypt.return_value = {"connector_instances": 2, "model_backends": 1}

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": [],
            "fernet_key_hash": _fernet_key_hash(old_fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "database.sql").write_text("-- SQL", encoding="utf-8")
        (tmp_path / "credentials_references.json").write_text(
            '{"connector_instances": [], "model_backends": []}', encoding="utf-8"
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "restore",
                str(tmp_path),
                "--yes",
                "--previous-fernet-key",
                old_fernet_key,
            ],
        )

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "Re-encrypting" in result.output
        assert "2 connector_instances re-encrypted" in result.output
        assert "1 model_backends re-encrypted" in result.output
        mock_re_encrypt.assert_called_once()
        args = mock_re_encrypt.call_args[0]
        assert args[2] == old_fernet_key  # previous ferret key
        assert args[3] == new_fernet_key  # current ferret key

    @patch("modulo.cli.backup._re_encrypt_credentials_sync")
    @patch("modulo.cli.backup._restore_checkpoint_blobs_sync")
    @patch("modulo.cli.backup._run_psql")
    @patch("modulo.cli.backup.get_settings")
    def test_restore_skips_re_encryption_when_key_unchanged(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        tmp_path: Path,
    ) -> None:
        fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.fernet_key = fernet_key
        mock_restore_blobs.return_value = 0

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": [],
            "fernet_key_hash": _fernet_key_hash(fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "database.sql").write_text("-- SQL", encoding="utf-8")
        (tmp_path / "credentials_references.json").write_text('{"connector_instances": []}', encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--yes"])

        assert result.exit_code == 0
        assert "FERNET_KEY unchanged" in result.output
        mock_re_encrypt.assert_not_called()

    @patch("modulo.cli.backup._re_encrypt_credentials_sync")
    @patch("modulo.cli.backup._restore_checkpoint_blobs_sync")
    @patch("modulo.cli.backup._run_psql")
    @patch("modulo.cli.backup.get_settings")
    def test_restore_confirmation_prompt(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        tmp_path: Path,
    ) -> None:
        fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_settings.return_value.fernet_key = fernet_key
        mock_restore_blobs.return_value = 0

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": [],
            "fernet_key_hash": _fernet_key_hash(fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "database.sql").write_text("-- SQL", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path)], input="y\n")

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert "OVERWRITE" in result.output
        assert "Restore complete" in result.output

    @patch("modulo.cli.backup.get_settings")
    def test_restore_failure(self, mock_settings: MagicMock, tmp_path: Path) -> None:
        mock_settings.return_value.fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

        (tmp_path / "backup-info.json").write_text(json.dumps({"timestamp": "now"}), encoding="utf-8")
        (tmp_path / "database.sql").write_text("", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(cli, ["restore", str(tmp_path), "--yes"])

        assert result.exit_code != 0

    @patch("modulo.cli.backup._re_encrypt_credentials_sync")
    @patch("modulo.cli.backup._restore_checkpoint_blobs_sync")
    @patch("modulo.cli.backup._run_psql")
    @patch("modulo.cli.backup.get_settings")
    def test_restore_skips_re_encryption_when_creds_json_missing(
        self,
        mock_settings: MagicMock,
        mock_psql: MagicMock,
        mock_restore_blobs: MagicMock,
        mock_re_encrypt: MagicMock,
        tmp_path: Path,
    ) -> None:
        new_fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        old_fernet_key = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        mock_settings.return_value.fernet_key = new_fernet_key
        mock_restore_blobs.return_value = 0

        manifest = {
            "timestamp": "2024-01-01T00:00:00+00:00",
            "backup_type": "full",
            "db_version": "PostgreSQL 16.0",
            "schema_versions": [],
            "fernet_key_hash": _fernet_key_hash(old_fernet_key),
        }
        (tmp_path / "backup-info.json").write_text(json.dumps(manifest), encoding="utf-8")
        (tmp_path / "database.sql").write_text("-- SQL", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "restore",
                str(tmp_path),
                "--yes",
                "--previous-fernet-key",
                old_fernet_key,
            ],
        )

        assert result.exit_code == 0, f"CLI failed: {result.output}"
        assert mock_re_encrypt.call_count == 0
