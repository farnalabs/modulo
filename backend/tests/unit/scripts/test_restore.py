"""Unit tests for restore.py — dry-run, decryption, extraction, verification."""

from __future__ import annotations

import os
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from scripts.restore import (
    decrypt_archive,
    extract_archive,
    get_db_url,
    hash_file,
    main,
    parse_args,
    pg_database_name,
    read_checksums,
    resolve_passphrase,
    restore_config,
    restore_postgres,
    verify_hashes,
)

openssl_available = shutil.which("openssl") is not None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.fixture
def sample_archive(tmp_dir):
    """Create a minimal valid tar.gz with known content."""
    content_dir = Path(tmp_dir) / "content"
    content_dir.mkdir(parents=True, exist_ok=True)
    Path(content_dir, "modulo.pgdump").write_text("fake-dump-content")
    Path(content_dir, "secrets.env").write_text("FERNET_KEY=test\n")
    Path(content_dir, "manifest.json").write_text('{"tool": "modulo-backup", "version": "1"}')
    # write checksums
    with Path(content_dir, "checksums.sha256").open("w") as f:
        for name in ("modulo.pgdump", "secrets.env", "manifest.json"):
            h = hash_file(Path(content_dir, name))
            f.write(f"{h}  {name}\n")

    archive_path = Path(tmp_dir, "backup.tar.gz")
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(content_dir, arcname=".")
    return archive_path, content_dir


# ---------------------------------------------------------------------------
# resolve_passphrase
# ---------------------------------------------------------------------------


def test_resolve_passphrase_from_arg():
    assert resolve_passphrase("secret123") == "secret123"


def test_resolve_passphrase_from_env(monkeypatch):
    monkeypatch.setenv("MODULO_BACKUP_PASSPHRASE", "env-pass")
    assert resolve_passphrase(None) == "env-pass"


def test_resolve_passphrase_prefers_arg_over_env(monkeypatch):
    monkeypatch.setenv("MODULO_BACKUP_PASSPHRASE", "env-pass")
    assert resolve_passphrase("arg-pass") == "arg-pass"


def test_resolve_passphrase_prompts_when_no_arg_or_env(monkeypatch):
    monkeypatch.delenv("MODULO_BACKUP_PASSPHRASE", raising=False)
    with patch("scripts.restore.getpass.getpass", return_value="typed-pass") as mock_prompt:
        assert resolve_passphrase(None) == "typed-pass"
    mock_prompt.assert_called_once_with("Restore passphrase: ")


# ---------------------------------------------------------------------------
# decrypt_archive
# ---------------------------------------------------------------------------


def test_decrypt_archive_missing_openssl_exits(tmp_dir, capsys):
    with (
        patch("scripts.restore.shutil.which", return_value=None),
        pytest.raises(SystemExit),
    ):
        decrypt_archive(Path(tmp_dir, "x.enc"), "pass", Path(tmp_dir, "x.tar.gz"))
    assert "openssl not found" in capsys.readouterr().out


def test_decrypt_archive_failure_exits(tmp_dir, capsys):
    proc = MagicMock()
    proc.returncode = 1
    proc.stderr = "bad decrypt"
    with (
        patch("scripts.restore.shutil.which", return_value="/usr/bin/openssl"),
        patch("scripts.restore.subprocess.run", return_value=proc),
        pytest.raises(SystemExit),
    ):
        decrypt_archive(Path(tmp_dir, "x.enc"), "pass", Path(tmp_dir, "x.tar.gz"))
    assert "Decryption failed: bad decrypt" in capsys.readouterr().out


def test_decrypt_archive_calls_openssl_with_expected_args(tmp_dir, capsys):
    enc_path = Path(tmp_dir, "in.enc")
    out_path = Path(tmp_dir, "out.tar.gz")
    proc = MagicMock()
    proc.returncode = 0
    with (
        patch("scripts.restore.subprocess.run", return_value=proc) as mock_run,
        patch("scripts.restore.shutil.which", return_value="/usr/bin/openssl"),
    ):
        decrypt_archive(enc_path, "pass", out_path)
    args = mock_run.call_args.args[0]
    assert args[0] == "openssl"
    assert "-d" in args
    assert enc_path in args
    assert out_path in args
    assert "pass:pass" in args
    assert "Decrypting archive..." in capsys.readouterr().out


@pytest.mark.skipif(not openssl_available, reason="openssl not installed")
def test_decrypt_archive_missing_input(tmp_dir, monkeypatch):
    monkeypatch.setenv("MODULO_BACKUP_PASSPHRASE", "test")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from scripts.restore import decrypt_archive as da
    from scripts.restore import resolve_passphrase as rp

    passphrase = rp(None)
    with pytest.raises(SystemExit):
        da("/nonexistent", passphrase, Path(tmp_dir, "out.tar.gz"))


# ---------------------------------------------------------------------------
# extract_archive
# ---------------------------------------------------------------------------


def test_extract_archive(tmp_dir, sample_archive):
    archive_path, _ = sample_archive
    extract_dir = Path(tmp_dir, "extracted")
    extract_dir.mkdir(parents=True, exist_ok=True)
    files = extract_archive(archive_path, extract_dir)
    assert "modulo.pgdump" in files
    assert "secrets.env" in files
    assert "manifest.json" in files
    assert Path(files["modulo.pgdump"]).exists()


def test_extract_archive_handles_subdirectories(tmp_dir):
    content = Path(tmp_dir, "content")
    Path(content, "sub").mkdir(parents=True)
    Path(content, "sub", "file.txt").write_text("x")
    Path(content, "root.txt").write_text("y")
    archive_path = Path(tmp_dir, "nested.tar.gz")
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(content, arcname=".")
    extract_dir = Path(tmp_dir, "out")
    extract_dir.mkdir(parents=True, exist_ok=True)

    files = extract_archive(archive_path, extract_dir)

    assert files["root.txt"] == str(extract_dir / "root.txt")
    # tar members always use "/" separators (POSIX spec) regardless of host
    # OS, so the dict key is "sub/file.txt" on every platform. Compare the
    # joined paths with os.path.normpath so Windows backslash vs. forward-slash
    # differences do not make the assertion platform-dependent.
    assert os.path.normpath(files["sub/file.txt"]) == os.path.normpath(str(extract_dir / "sub" / "file.txt"))
    assert Path(files["sub/file.txt"]).exists()


# ---------------------------------------------------------------------------
# read_checksums
# ---------------------------------------------------------------------------


def test_read_checksums(tmp_dir, sample_archive):
    _, content_dir = sample_archive
    checksums = read_checksums(content_dir)
    assert len(checksums) == 3
    assert "modulo.pgdump" in checksums
    for h in checksums.values():
        assert len(h) == 64  # SHA-256 hex


def test_read_checksums_missing(tmp_dir):
    assert not read_checksums(tmp_dir)


def test_read_checksums_skips_malformed_and_blank_lines(tmp_dir):
    cs = Path(tmp_dir, "checksums.sha256")
    Path(cs).write_text("not-a-checksum\n\n" + "a" * 64 + "  good.txt\n")
    assert read_checksums(tmp_dir) == {"good.txt": "a" * 64}


# ---------------------------------------------------------------------------
# verify_hashes
# ---------------------------------------------------------------------------


def test_verify_hashes_ok(tmp_dir, sample_archive):
    _, content_dir = sample_archive
    files = {
        "modulo.pgdump": Path(content_dir, "modulo.pgdump"),
        "secrets.env": Path(content_dir, "secrets.env"),
        "manifest.json": Path(content_dir, "manifest.json"),
    }
    assert verify_hashes(content_dir, files) is True


def test_verify_hashes_fails_on_corrupted(tmp_dir, sample_archive):
    _, content_dir = sample_archive
    pg_path = Path(content_dir, "modulo.pgdump")
    # corrupt the file
    Path(pg_path).write_text("tampered content")
    files = {"modulo.pgdump": pg_path}
    assert verify_hashes(content_dir, files) is False


def test_verify_hashes_returns_true_without_checksums(tmp_dir):
    target = Path(tmp_dir, "a.txt")
    Path(target).write_text("x")
    assert verify_hashes(tmp_dir, {"a.txt": target}) is True


def test_verify_hashes_unchecked_files_do_not_fail(tmp_dir):
    known = Path(tmp_dir, "known.txt")
    extra = Path(tmp_dir, "extra.txt")
    Path(known).write_text("aaa")
    Path(extra).write_text("bbb")
    Path(tmp_dir, "checksums.sha256").write_text(f"{hash_file(known)}  known.txt\n")
    files = {"known.txt": known, "extra.txt": extra}
    assert verify_hashes(tmp_dir, files) is True


# ---------------------------------------------------------------------------
# get_db_url
# ---------------------------------------------------------------------------


def test_get_db_url_from_arg():
    assert get_db_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


def test_get_db_url_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://env:secret@host/db")
    assert get_db_url(None) == "postgresql://env:secret@host/db"


def test_get_db_url_missing(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        get_db_url(None)


# ---------------------------------------------------------------------------
# pg_database_name
# ---------------------------------------------------------------------------


def test_pg_database_name_extracts_path():
    assert pg_database_name("postgresql://u:p@h/mydb") == "mydb"


def test_pg_database_name_strips_query_string():
    assert pg_database_name("postgresql://u:p@h/mydb?sslmode=require") == "mydb"


def test_pg_database_name_root_is_empty():
    assert not pg_database_name("postgresql://u:p@h/")


# ---------------------------------------------------------------------------
# restore_postgres
# ---------------------------------------------------------------------------


def test_restore_postgres_missing_dump_exits(tmp_dir, capsys):
    with pytest.raises(SystemExit):
        restore_postgres(tmp_dir, "postgresql://u:p@h/db", "pg_restore")
    assert "modulo.pgdump not found" in capsys.readouterr().out


def test_restore_postgres_success(tmp_dir, capsys):
    Path(tmp_dir, "modulo.pgdump").write_text("dump")
    ok = MagicMock(returncode=0, stderr="")
    with patch("scripts.restore.subprocess.run", return_value=ok) as mock_run:
        restore_postgres(tmp_dir, "postgresql://u:p@h/db", "pg_restore")
    assert mock_run.call_count == 4
    out = capsys.readouterr().out
    assert "Restoring Postgres to database 'db'" in out
    assert "Postgres restore complete." in out


def test_restore_postgres_createdb_failure_exits(tmp_dir, capsys):
    Path(tmp_dir, "modulo.pgdump").write_text("dump")
    ok = MagicMock(returncode=0, stderr="")
    fail = MagicMock(returncode=1, stderr="createdb: database creation failed")
    with (
        patch("scripts.restore.subprocess.run", side_effect=[ok, ok, fail]) as mock_run,
        pytest.raises(SystemExit),
    ):
        restore_postgres(tmp_dir, "postgresql://u:p@h/db", "pg_restore")
    assert "createdb failed: createdb: database creation failed" in capsys.readouterr().out
    assert mock_run.call_count == 3


# ---------------------------------------------------------------------------
# restore_config
# ---------------------------------------------------------------------------


def test_restore_config_missing_secrets_warns(tmp_dir, capsys):
    restore_config(tmp_dir)
    assert "WARNING: secrets.env not found" in capsys.readouterr().out


def test_restore_config_prints_exports(tmp_dir, capsys):
    Path(tmp_dir, "secrets.env").write_text("FERNET_KEY=abc\nSECRET_KEY=def\n")
    restore_config(tmp_dir)
    out = capsys.readouterr().out
    assert "    export FERNET_KEY=abc" in out
    assert "    export SECRET_KEY=def" in out


def test_restore_config_prints_manifest_metadata(tmp_dir, capsys):
    import json

    Path(tmp_dir, "secrets.env").write_text("FERNET_KEY=abc\n")
    Path(tmp_dir, "manifest.json").write_text(json.dumps({"created_at": "2026-01-01T00:00:00Z", "version": "1"}))
    restore_config(tmp_dir)
    out = capsys.readouterr().out
    assert "Backup created at: 2026-01-01T00:00:00Z" in out
    assert "Tool version: 1" in out


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def test_parse_args_requires_input(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["restore.py"])
    with pytest.raises(SystemExit):
        parse_args()


def test_parse_args_flags(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["restore.py", "-i", "x.enc", "--dry-run"])
    args = parse_args()
    assert args.input == "x.enc"
    assert args.dry_run is True
    assert args.full is False
    assert args.data_only is False
    assert args.config_only is False


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


async def test_main_missing_input_exits(tmp_dir):
    ns = MagicMock()
    ns.input = Path(tmp_dir, "nonexistent.enc")
    with (
        patch("scripts.restore.parse_args", return_value=ns),
        pytest.raises(SystemExit),
    ):
        await main()


async def test_main_mode_conflict_exits(tmp_dir, capsys):
    input_path = Path(tmp_dir, "backup.enc")
    Path(input_path).write_text("x")  # noqa: ASYNC240 — trivial 1-byte test setup
    ns = MagicMock()
    ns.input = input_path
    ns.full = True
    ns.data_only = True
    ns.config_only = False
    ns.dry_run = False
    with (
        patch("scripts.restore.parse_args", return_value=ns),
        pytest.raises(SystemExit),
    ):
        await main()
    assert "choose only one of --full, --data-only, --config-only" in capsys.readouterr().out


async def test_main_no_mode_exits(tmp_dir, capsys):
    input_path = Path(tmp_dir, "backup.enc")
    Path(input_path).write_text("x")  # noqa: ASYNC240 — trivial 1-byte test setup
    ns = MagicMock()
    ns.input = input_path
    ns.full = False
    ns.data_only = False
    ns.config_only = False
    ns.dry_run = False
    with (
        patch("scripts.restore.parse_args", return_value=ns),
        pytest.raises(SystemExit),
    ):
        await main()
    assert "specify --dry-run, --full, --data-only, or --config-only" in capsys.readouterr().out


async def test_main_empty_passphrase_exits(tmp_dir, capsys):
    input_path = Path(tmp_dir, "backup.enc")
    Path(input_path).write_text("x")  # noqa: ASYNC240 — trivial 1-byte test setup
    ns = MagicMock()
    ns.input = input_path
    ns.full = False
    ns.data_only = False
    ns.config_only = False
    ns.dry_run = True
    with (
        patch("scripts.restore.parse_args", return_value=ns),
        patch("scripts.restore.resolve_passphrase", return_value=""),
        pytest.raises(SystemExit),
    ):
        await main()
    assert "passphrase cannot be empty" in capsys.readouterr().out


async def test_main_dry_run_success(tmp_dir, capsys):
    input_path = Path(tmp_dir, "backup.enc")
    Path(input_path).write_text("x")  # noqa: ASYNC240 — trivial 1-byte test setup
    ns = MagicMock()
    ns.input = input_path
    ns.full = False
    ns.data_only = False
    ns.config_only = False
    ns.dry_run = True
    with (
        patch("scripts.restore.parse_args", return_value=ns),
        patch("scripts.restore.resolve_passphrase", return_value="pass"),
        patch("scripts.restore.decrypt_archive") as mock_dec,
        patch("scripts.restore.extract_archive", return_value={"a": Path(tmp_dir, "a")}) as mock_ext,
        patch("scripts.restore.verify_hashes", return_value=True) as mock_verify,
    ):
        await main()
    mock_dec.assert_called_once()
    mock_ext.assert_called_once()
    mock_verify.assert_called_once()
    assert "Dry-run: archive verified successfully." in capsys.readouterr().out
