"""Unit tests for backup CLI paths (mocked psycopg/subprocess, no DB)."""

import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import click
import pytest
from cryptography.fernet import Fernet

import modulo.cli.backup as backup_mod
from modulo.cli.backup import (
    _check_tool,
    _export_checkpoint_blobs_sync,
    _export_checkpoint_writes_sync,
    _export_checkpoints_sync,
    _export_credentials_references_sync,
    _fernet_key_hash,
    _file_checksum,
    _get_backend_dir,
    _get_db_version,
    _get_schema_versions,
    _human_size,
    _parse_org_id,
    _preview_restore,
    _print_size,
    _re_encrypt_credentials_sync,
    _resolve_url,
    _restore_checkpoint_blobs_sync,
    _restore_checkpoint_writes_sync,
    _restore_checkpoints_sync,
    _run_pg_dump,
    _run_psql,
    _serialise_export_row,
    _serialise_for_json,
)

_OLD_KEY = Fernet.generate_key().decode()
_NEW_KEY = Fernet.generate_key().decode()
_ROW_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class TestPureHelpers:
    def test_resolve_url_strips_async_drivers(self) -> None:
        assert _resolve_url("postgresql+asyncpg://u:p@h/db") == "postgresql://u:p@h/db"
        assert _resolve_url("postgresql+psycopg://u:p@h/db") == "postgresql://u:p@h/db"
        assert _resolve_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"

    def test_fernet_key_hash_truncates(self) -> None:
        import hashlib

        assert _fernet_key_hash("key") == hashlib.sha256(b"key").hexdigest()[:16]
        assert len(_fernet_key_hash("key")) == 16

    def test_file_checksum(self, tmp_path: Path) -> None:
        import hashlib

        path = tmp_path / "f.bin"
        path.write_bytes(b"payload")
        assert _file_checksum(path) == hashlib.sha256(b"payload").hexdigest()

    def test_serialise_for_json(self) -> None:
        assert _serialise_for_json(uuid.UUID(int=1)) == str(uuid.UUID(int=1))
        assert _serialise_for_json(b"\xff") == "ff"
        assert _serialise_for_json(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00+00:00"
        assert _serialise_for_json("plain") == "plain"

    def test_serialise_export_row(self) -> None:
        row = {
            "id": uuid.UUID(int=2),
            "blob": memoryview(b"\x01\x02"),
            "ts": datetime(2026, 1, 1, tzinfo=UTC),
            "plain": "x",
        }
        result = _serialise_export_row(row)
        assert result["id"] == str(uuid.UUID(int=2))
        assert result["blob"] == "0102"
        assert result["ts"] == "2026-01-01T00:00:00+00:00"
        assert result["plain"] == "x"

    def test_parse_org_id(self) -> None:
        org_id = uuid.UUID(int=3)
        assert _parse_org_id(str(org_id)) == org_id
        assert _parse_org_id(None) is None
        with pytest.raises(RuntimeError, match="Invalid organisation_id"):
            _parse_org_id("nope")

    def test_backend_dir_is_repo_root(self) -> None:
        backend_dir = _get_backend_dir()
        assert (backend_dir / "pyproject.toml").exists()
        assert (backend_dir / "alembic.ini").exists()

    def test_human_size_units(self) -> None:
        assert _human_size(512.0) == "512.0 B"
        assert _human_size(2048.0) == "2.0 KB"
        assert _human_size(3 * 1024 * 1024) == "3.0 MB"
        assert _human_size(2.0 * 1024**3) == "2.0 GB"
        assert _human_size(3.0 * 1024**4) == "3.0 TB"

    def test_print_size(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        (tmp_path / "a.txt").write_text("hello")
        _print_size(tmp_path)
        assert "Total size" in capsys.readouterr().out

    def test_print_size_logs_on_oserror(self, capsys: pytest.CaptureFixture[str]) -> None:
        boom = MagicMock()
        boom.rglob = MagicMock(side_effect=OSError("perm"))
        _print_size(boom)
        assert "Total size" not in capsys.readouterr().out


class TestURLAndVersionHelpers:
    def test_schema_versions_reads_heads(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("")
        (tmp_path / "alembic.ini").write_text("")
        monkeypatch.setattr(backup_mod, "_get_backend_dir", lambda: tmp_path)
        fake_script = MagicMock()
        fake_script.get_heads.return_value = ["0199_split_head", "0171_runs"]
        fake_sd = MagicMock(from_config=MagicMock(return_value=fake_script))
        fake_config = MagicMock(kwargs={})
        with patch.dict(
            "sys.modules",
            {
                "alembic.config": _mod(Config=lambda path: fake_config),
                "alembic.script": _mod(ScriptDirectory=fake_sd),
            },
        ):
            assert _get_schema_versions() == ["0171_runs", "0199_split_head"]
        fake_script.get_heads.assert_called_once()

    def test_schema_versions_unknown_when_ini_missing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(backup_mod, "_get_backend_dir", lambda: tmp_path)
        assert _get_schema_versions() == ["unknown"]

    def test_schema_versions_unknown_on_error(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        (tmp_path / "alembic.ini").write_text("")
        monkeypatch.setattr(backup_mod, "_get_backend_dir", lambda: tmp_path)
        stubs = {"alembic.config": _mod(Config=None), "alembic.script": _mod(ScriptDirectory=None)}
        with patch.dict("sys.modules", stubs):
            assert _get_schema_versions() == ["unknown"]

    def test_db_version_success(self) -> None:
        row = MagicMock()
        assert row is not None
        exec_result = MagicMock()
        exec_result.fetchone.return_value = ["PostgreSQL 16.2"]
        conn = MagicMock()
        conn.execute.return_value = exec_result
        conn_cm = MagicMock()
        conn_cm.__enter__.return_value = conn
        fake_psycopg = MagicMock(connect=MagicMock(return_value=conn_cm))
        with patch.object(backup_mod, "psycopg", fake_psycopg):
            assert _get_db_version("postgresql://x") == "PostgreSQL 16.2"

    def test_db_version_unknown_when_psycopg_none(self) -> None:
        with patch.object(backup_mod, "psycopg", None):
            assert _get_db_version("postgresql://x") == "unknown"

    def test_db_version_unknown_on_connect_failure(self) -> None:
        boom = MagicMock(connect=MagicMock(side_effect=RuntimeError("no route")))
        with patch.object(backup_mod, "psycopg", boom):
            assert _get_db_version("postgresql://x") == "unknown"


def _mod(**attrs: object) -> MagicMock:
    module = MagicMock()
    for name, value in attrs.items():
        setattr(module, name, value)
    return module


class TestPsqlDumpHelpers:
    def test_check_tool_missing_raises(self) -> None:
        with patch.object(shutil, "which", return_value=None), pytest.raises(RuntimeError, match="pg_dump"):
            _check_tool("pg_dump")

    def test_check_tool_found(self) -> None:
        with patch.object(shutil, "which", return_value="/usr/bin/pg_dump") as which:
            _check_tool("pg_dump")
        which.assert_called_once_with("pg_dump")

    def test_run_pg_dump_success(self, tmp_path: Path) -> None:
        out = tmp_path / "database.sql"
        completed = MagicMock(returncode=0)
        with (
            patch.object(shutil, "which", return_value="/usr/bin/pg_dump"),
            patch.object(subprocess, "run", return_value=completed) as run,
        ):
            _run_pg_dump("postgresql://x", out, timeout=7)
        run.assert_called_once()
        assert out.parent == tmp_path

    def test_run_pg_dump_nonzero(self, tmp_path: Path) -> None:
        out = tmp_path / "database.sql"
        completed = MagicMock(returncode=1, stderr=b"boom")
        with (
            patch.object(shutil, "which", return_value="/usr/bin/pg_dump"),
            patch.object(subprocess, "run", return_value=completed),
            pytest.raises(RuntimeError, match="pg_dump failed: boom"),
        ):
            _run_pg_dump("postgresql://x", out, timeout=10)

    def test_run_pg_dump_timeout(self, tmp_path: Path) -> None:
        out = tmp_path / "database.sql"
        with (
            patch.object(shutil, "which", return_value="/usr/bin/pg_dump"),
            patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="pg_dump", timeout=3)),
            pytest.raises(RuntimeError, match="timed out"),
        ):
            _run_pg_dump("postgresql://x", out, timeout=3)

    def test_run_psql_success_and_failure(self, tmp_path: Path) -> None:
        sql = tmp_path / "database.sql"
        sql.write_text("SELECT 1;")
        completed = MagicMock(returncode=0)
        with (
            patch.object(shutil, "which", return_value="/usr/bin/psql"),
            patch.object(subprocess, "run", return_value=completed) as run,
        ):
            _run_psql("postgresql://x", sql, timeout=10)
        run.assert_called_once()

        failing = MagicMock(returncode=2, stderr=b"server gone")
        with (
            patch.object(shutil, "which", return_value="/usr/bin/psql"),
            patch.object(subprocess, "run", return_value=failing),
            pytest.raises(RuntimeError, match="psql restore failed: server gone"),
        ):
            _run_psql("postgresql://x", sql, timeout=10)

    def test_run_psql_timeout(self, tmp_path: Path) -> None:
        sql = tmp_path / "database.sql"
        sql.write_text("SELECT 1;")
        with (
            patch.object(shutil, "which", return_value="/usr/bin/psql"),
            patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="psql", timeout=3)),
            pytest.raises(RuntimeError, match="timed out"),
        ):
            _run_psql("postgresql://x", sql, timeout=5)


def _psycopg_stub(cursor: MagicMock) -> MagicMock:
    fake_conn = MagicMock()
    fake_conn.cursor.return_value.__enter__.return_value = cursor
    conn_ctx = MagicMock()
    conn_ctx.__enter__.return_value = fake_conn
    return MagicMock(connect=MagicMock(return_value=conn_ctx))


class TestSyncExports:
    def test_psycopg_unavailable_raises(self) -> None:
        with patch.object(backup_mod, "psycopg", None):
            for fn, args in (
                (_export_checkpoint_blobs_sync, ()),
                (_export_checkpoints_sync, ()),
                (_export_checkpoint_writes_sync, ()),
                (_export_credentials_references_sync, ()),
                (_restore_checkpoint_blobs_sync, ([],)),
                (_restore_checkpoints_sync, ([],)),
                (_restore_checkpoint_writes_sync, ([],)),
                (_re_encrypt_credentials_sync, ({}, _OLD_KEY, _NEW_KEY)),
            ):
                with pytest.raises(RuntimeError, match="psycopg library is not available"):
                    fn("postgresql://x", *args)

    def test_export_rows_serialised(self) -> None:
        row = {"organisation_id": _ROW_ID, "thread_id": "t1", "blob": b"\x01"}
        cursor = MagicMock()
        cursor.__iter__.return_value = iter([row])
        with patch.object(backup_mod, "psycopg", _psycopg_stub(cursor)):
            result = _export_checkpoint_blobs_sync("postgresql://x")
        assert result[0]["organisation_id"] == str(_ROW_ID)
        assert result[0]["blob"] == "01"

    def test_checkpoints_export(self) -> None:
        cursor = MagicMock()
        cursor.__iter__.return_value = iter([])
        fake_conn = MagicMock()
        fake_conn.cursor.return_value.__enter__.return_value = cursor
        conn_ctx = MagicMock()
        conn_ctx.__enter__.return_value = fake_conn
        fake_psycopg = MagicMock(connect=MagicMock(return_value=conn_ctx))
        with patch.object(backup_mod, "psycopg", fake_psycopg):
            assert not _export_checkpoints_sync("postgresql://x")
            assert not _export_checkpoint_writes_sync("postgresql://x")

    def test_connections_commit_and_execute(self) -> None:
        cursor = MagicMock()
        fake_conn = MagicMock()
        fake_conn.cursor.return_value.__enter__.return_value = cursor
        conn_ctx = MagicMock()
        conn_ctx.__enter__.return_value = fake_conn
        fake_psycopg = MagicMock(connect=MagicMock(return_value=conn_ctx))
        with patch.object(backup_mod, "psycopg", fake_psycopg):
            assert _restore_checkpoint_blobs_sync("postgresql://x", []) == 0
            assert _restore_checkpoints_sync("postgresql://x", []) == 0
            assert _restore_checkpoint_writes_sync("postgresql://x", []) == 0
            assert cursor.execute.call_count == 3

    def test_creds_reference_serialisation(self) -> None:
        connector_rows = [
            {"id": _ROW_ID, "organisation_id": uuid.UUID(int=4), "credentials_ciphertext": b"\xaa"},
            {"id": uuid.UUID(int=5), "organisation_id": None, "credentials_ciphertext": "plain"},
        ]
        creds_cursor = MagicMock()

        def _execute_per_table(sql: str, *_args: object) -> None:
            creds_cursor.__iter__.return_value = iter(list(connector_rows) if "connector_instances" in sql else [])

        creds_cursor.execute.side_effect = _execute_per_table
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = creds_cursor
        conn_ctx = MagicMock()
        conn_ctx.__enter__.return_value = conn
        fake_psycopg = MagicMock(connect=MagicMock(return_value=conn_ctx))
        with patch.object(backup_mod, "psycopg", fake_psycopg):
            result = _export_credentials_references_sync("postgresql://x")
        assert result["connector_instances"][0]["id"] == str(_ROW_ID)
        assert result["connector_instances"][0]["organisation_id"] == str(uuid.UUID(int=4))
        assert result["connector_instances"][0]["credentials_ciphertext"] == "aa"
        assert result["connector_instances"][1]["organisation_id"] is None
        assert not result["model_backends"]


class TestRestoreSyncFunctions:
    @staticmethod
    def _psycopg_capture() -> tuple[MagicMock, MagicMock]:
        cursor = MagicMock()
        fake_conn = MagicMock()
        fake_conn.cursor.return_value.__enter__.return_value = cursor
        conn_ctx = MagicMock()
        conn_ctx.__enter__.return_value = fake_conn
        fake_psycopg = MagicMock(connect=MagicMock(return_value=conn_ctx))
        return fake_psycopg, cursor

    def test_restore_blobs_hex_parsing(self) -> None:
        fake_psycopg, cursor = self._psycopg_capture()
        rows = [
            {"organisation_id": str(_ROW_ID), "blob": None},
            {"organisation_id": str(_ROW_ID), "blob": "beef"},
            {"organisation_id": "", "blob": ""},
        ]
        with patch.object(backup_mod, "psycopg", fake_psycopg):
            result = _restore_checkpoint_blobs_sync("postgresql://x", rows)
        assert result == 3
        assert cursor.execute.call_count == 1 + 3

    def test_restore_checkpoints_and_writes(self) -> None:
        fake_psycopg, cursor = self._psycopg_capture()
        rows = [{"organisation_id": str(_ROW_ID), "blob": None}]
        with patch.object(backup_mod, "psycopg", fake_psycopg):
            assert _restore_checkpoints_sync("postgresql://x", rows) == 1
            assert _restore_checkpoint_writes_sync("postgresql://x", rows) == 1
        assert cursor.execute.call_count == 2 + 2

    def test_re_encrypt_success_round_trip(self) -> None:
        old_f = Fernet(_OLD_KEY.encode())
        ciphertext = old_f.encrypt(b"super-secret")
        creds = {
            "connector_instances": [
                {"id": str(_ROW_ID), "credentials_ciphertext": ciphertext.hex()},
            ],
            "model_backends": [
                {"id": "not-a-uuid", "credentials_ciphertext": ciphertext.hex()},
                {"id": str(_ROW_ID), "credentials_ciphertext": ""},
            ],
        }
        fake_psycopg, cursor = self._psycopg_capture()
        with patch.object(backup_mod, "psycopg", fake_psycopg):
            counts = _re_encrypt_credentials_sync("postgresql://x", creds, _OLD_KEY, _NEW_KEY)
        assert counts == {"connector_instances": 1, "model_backends": 0}
        assert cursor.execute.call_count == 1

    def test_re_encrypt_unknown_table_skipped(self) -> None:
        fake_psycopg, _cursor = self._psycopg_capture()
        with patch.object(backup_mod, "psycopg", fake_psycopg):
            counts = _re_encrypt_credentials_sync(
                "postgresql://x",
                {"myseat": [{"id": str(_ROW_ID), "credentials_ciphertext": "ab"}]},
                _OLD_KEY,
                _NEW_KEY,
            )
        assert counts == {}

    def test_re_encrypt_invalid_ciphertext_raises(self) -> None:
        fake_psycopg, _cursor = self._psycopg_capture()
        with (
            patch.object(backup_mod, "psycopg", fake_psycopg),
            pytest.raises(RuntimeError, match="previous-fernet-key may be wrong"),
        ):
            _re_encrypt_credentials_sync(
                "postgresql://x",
                {"connector_instances": [{"id": str(_ROW_ID), "credentials_ciphertext": "ab"}]},
                _OLD_KEY,
                _NEW_KEY,
            )


class TestPreviewRestore:
    @staticmethod
    def _manifest() -> dict[str, Any]:
        return {"fernet_key_hash": "hash-current", "backup_type": "full"}

    @staticmethod
    def _settings(key_hash: str = "hash-current") -> SimpleNamespace:
        return SimpleNamespace(fernet_key="k", fernet_key_hash=key_hash)

    def test_full_dry_run_output(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        settings = self._settings()
        settings.fernet_key = "current-key"
        files = {
            "database.sql": "SQL",
            "checkpoints.json": "[[], []]",
            "checkpoint_blobs.json": "[1, 2]",
            "checkpoint_writes.json": "[]",
            "credentials_references.json": "{}",
        }
        for name, content in files.items():
            (tmp_path / name).write_text(content)
        manifest: dict[str, Any] = {"fernet_key_hash": backup_mod._fernet_key_hash(settings.fernet_key)}
        with patch("modulo.cli.backup._fernet_key_hash", return_value=manifest["fernet_key_hash"]):
            _preview_restore(tmp_path, manifest, settings, None)
        out = capsys.readouterr().out
        assert "no changes were made" in out
        assert "psql from database.sql" in out
        assert "2 checkpoint blob records" in out

    def test_key_changed_without_previous_key_raises(self, tmp_path: Path) -> None:
        settings = self._settings()
        settings.fernet_key = "new-key"
        (tmp_path / "credentials_references.json").write_text("{}")
        manifest = {"fernet_key_hash": "old-hash"}
        with patch("modulo.cli.backup._fernet_key_hash", return_value="current-hash") as hfn:
            hfn.side_effect = None
            with pytest.raises(click.ClickException, match="previous-fernet-key"):
                _preview_restore(tmp_path, manifest, settings, None)

    def test_key_changed_with_previous_key_lines(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        settings = self._settings()
        settings.fernet_key = "new-key"
        (tmp_path / "credentials_references.json").write_text(
            '{"model_backends": [{"credentials_ciphertext": "aa"}, {"credentials_ciphertext": ""}]}'
        )
        manifest = {"fernet_key_hash": "old-hash"}
        with patch(
            "modulo.cli.backup._fernet_key_hash",
            return_value="current-hash",
        ):
            _preview_restore(tmp_path, manifest, settings, "old-key")
        out = capsys.readouterr().out
        assert "1 model_backends credentials" in out

    def test_key_changed_with_no_ciphertext_lines(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        settings = self._settings()
        settings.fernet_key = "new-key"
        (tmp_path / "credentials_references.json").write_text('{"model_backends": [{"credentials_ciphertext": ""}]}')
        manifest = {"fernet_key_hash": "old-hash"}
        with patch(
            "modulo.cli.backup._fernet_key_hash",
            return_value="current-hash",
        ):
            _preview_restore(tmp_path, manifest, settings, "old-key")
        out = capsys.readouterr().out
        assert "nothing to re-encrypt" in out

    def test_missing_files_messages(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        settings = self._settings()
        with patch("modulo.cli.backup._fernet_key_hash", return_value="same"):
            _preview_restore(tmp_path, {"fernet_key_hash": "same"}, settings, None)
        out = capsys.readouterr().out
        assert "No database.sql found" in out
        assert "No checkpoint_blobs.json found" in out
        assert "No credentials_references.json found" in out
