"""Unit tests for backup.py — archive creation, encryption, metadata."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

for parent in Path(__file__).resolve().parents:
    if (parent / "scripts" / "backup.py").exists():
        sys.path.insert(0, str(parent))
        break
else:
    raise RuntimeError("Could not find repo root (scripts/backup.py)")
from scripts.backup import (  # noqa: E402
    check_disk_space,
    collect_secrets,
    create_archive,
    encrypt_archive,
    get_db_url,
    get_org_id,
    hash_file,
    main,
    parse_args,
    resolve_passphrase,
    run_pg_dump,
    write_checksums,
)

openssl_available = shutil.which("openssl") is not None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_manifest_dir():
    with tempfile.TemporaryDirectory() as d:
        yield d


# ---------------------------------------------------------------------------
# resolve_passphrase
# ---------------------------------------------------------------------------


def test_resolve_passphrase_from_arg():
    assert resolve_passphrase("supersecret") == "supersecret"


def test_resolve_passphrase_from_env(monkeypatch):
    monkeypatch.setenv("MODULO_BACKUP_PASSPHRASE", "envpass")
    assert resolve_passphrase(None) == "envpass"


def test_resolve_passphrase_prefers_arg_over_env(monkeypatch):
    monkeypatch.setenv("MODULO_BACKUP_PASSPHRASE", "envpass")
    assert resolve_passphrase("argpass") == "argpass"


def test_resolve_passphrase_prompts_when_no_arg_or_env(monkeypatch):
    monkeypatch.delenv("MODULO_BACKUP_PASSPHRASE", raising=False)
    with patch("scripts.backup.getpass.getpass", return_value="typed-pass") as mock_prompt:
        assert resolve_passphrase(None) == "typed-pass"
    mock_prompt.assert_called_once_with("Backup passphrase: ")


# ---------------------------------------------------------------------------
# check_disk_space
# ---------------------------------------------------------------------------


def test_check_disk_space_passes_when_sufficient(tmp_manifest_dir, capsys):
    with patch("scripts.backup.shutil.disk_usage") as mock_usage:
        mock_usage.return_value.free = 10 * 1024**3
        check_disk_space(tmp_manifest_dir, 1)  # should not raise SystemExit
    out = capsys.readouterr().out
    assert "Disk space:" in out
    assert "10.0 GB free" in out


def test_check_disk_space_exits_when_insufficient(tmp_manifest_dir, capsys):
    with patch("scripts.backup.shutil.disk_usage") as mock_usage:
        mock_usage.return_value.free = 0.5 * 1024**3
        with pytest.raises(SystemExit):
            check_disk_space(tmp_manifest_dir, 1)
    assert "Insufficient disk space" in capsys.readouterr().out


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
# run_pg_dump
# ---------------------------------------------------------------------------


async def test_run_pg_dump_success(tmp_manifest_dir):
    output = str(Path(tmp_manifest_dir) / "dump.pgdump")
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(None, b""))
    with patch("scripts.backup.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        await run_pg_dump("postgresql://u:p@h/db", "pg_dump", output)
    args = mock_exec.await_args.args
    assert args[0] == "pg_dump"
    assert "--format=custom" in args
    assert output in args
    assert "postgresql://u:p@h/db" in args


async def test_run_pg_dump_failure_exits(tmp_manifest_dir, capsys):
    proc = MagicMock()
    proc.returncode = 1
    proc.communicate = AsyncMock(return_value=(None, b"connection failed"))
    with (
        patch("scripts.backup.asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        pytest.raises(SystemExit),
    ):
        await run_pg_dump("postgresql://u:p@h/db", "pg_dump", str(Path(tmp_manifest_dir) / "x"))
    assert "pg_dump failed: connection failed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# collect_secrets
# ---------------------------------------------------------------------------


def test_collect_secrets_creates_env_file(tmp_manifest_dir, monkeypatch):
    monkeypatch.setenv("FERNET_KEY", "test-fernet-key")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    files = collect_secrets(tmp_manifest_dir)

    secrets_path = str(Path(tmp_manifest_dir) / "secrets.env")
    assert secrets_path in files
    assert Path(secrets_path).exists()

    with Path(secrets_path).open() as f:
        content = f.read()
    assert "FERNET_KEY=test-fernet-key" in content
    assert "SECRET_KEY=test-secret-key" in content


def test_collect_secrets_writes_all_keys_with_empty_defaults(tmp_manifest_dir, monkeypatch):
    for key in ("FERNET_KEY", "SECRET_KEY", "DATABASE_URL", "MODULO_PUBLIC_URL", "REDIS_URL"):
        monkeypatch.delenv(key, raising=False)
    files = collect_secrets(tmp_manifest_dir)
    content = Path(tmp_manifest_dir, "secrets.env").read_text()
    lines = dict(line.split("=", 1) for line in content.strip().splitlines())
    assert set(lines) == {"FERNET_KEY", "SECRET_KEY", "DATABASE_URL", "MODULO_PUBLIC_URL", "REDIS_URL"}
    assert all(value == "" for value in lines.values())
    assert str(Path(tmp_manifest_dir) / "secrets.env") in files


def test_collect_secrets_creates_manifest(tmp_manifest_dir):
    files = collect_secrets(tmp_manifest_dir)
    manifest_path = str(Path(tmp_manifest_dir) / "manifest.json")
    assert manifest_path in files
    assert Path(manifest_path).exists()

    with Path(manifest_path).open() as f:
        manifest = json.load(f)
    assert manifest["tool"] == "modulo-backup"
    assert manifest["version"] == "1"
    assert "created_at" in manifest


# ---------------------------------------------------------------------------
# hash_file, write_checksums
# ---------------------------------------------------------------------------


def test_hash_file_matches_known_sha256(tmp_manifest_dir):
    path = str(Path(tmp_manifest_dir) / "known.txt")
    Path(path).write_text("hello world")
    assert hash_file(path) == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


def test_hash_file_consistent(tmp_manifest_dir):
    path = str(Path(tmp_manifest_dir) / "test.txt")
    Path(path).write_text("hello world")
    h1 = hash_file(path)
    h2 = hash_file(path)
    assert h1 == h2


def test_write_checksums(tmp_manifest_dir):
    a = str(Path(tmp_manifest_dir) / "a.dat")
    b = str(Path(tmp_manifest_dir) / "b.dat")
    Path(a).write_text("aaa")
    Path(b).write_text("bbb")
    cs_path = write_checksums(tmp_manifest_dir, [a, b])
    assert Path(cs_path).exists()

    with Path(cs_path).open() as f:
        lines = f.read().strip().split("\n")
    assert len(lines) == 2
    for line in lines:
        assert "  " in line
        h, name = line.split("  ", 1)
        assert len(h) == 64  # SHA-256 hex
        assert name in ("a.dat", "b.dat")


def test_write_checksums_sorted_by_name(tmp_manifest_dir):
    b = str(Path(tmp_manifest_dir) / "b.dat")
    a = str(Path(tmp_manifest_dir) / "a.dat")
    Path(a).write_text("aaa")
    Path(b).write_text("bbb")
    write_checksums(tmp_manifest_dir, [b, a])
    lines = Path(tmp_manifest_dir, "checksums.sha256").read_text().strip().splitlines()
    assert [line.split("  ", 1)[1] for line in lines] == ["a.dat", "b.dat"]


# ---------------------------------------------------------------------------
# create_archive
# ---------------------------------------------------------------------------


def test_create_archive_packs_files(tmp_manifest_dir):
    Path(tmp_manifest_dir, "a.txt").write_text("aaa")
    Path(tmp_manifest_dir, "b.txt").write_text("bbb")
    output = str(Path(tmp_manifest_dir) / "backup.tar.gz")
    result = create_archive(tmp_manifest_dir, output)
    assert result == output
    assert Path(output).exists()
    assert tarfile.is_tarfile(output)


def test_create_archive_strips_only_enc_suffix(tmp_manifest_dir):
    Path(tmp_manifest_dir, "a.txt").write_text("aaa")
    output = str(Path(tmp_manifest_dir) / "note.enc")
    result = create_archive(tmp_manifest_dir, output)
    assert result == str(Path(tmp_manifest_dir) / "note")
    assert Path(result).exists()


def test_create_archive_keeps_output_without_enc_suffix(tmp_manifest_dir):
    Path(tmp_manifest_dir, "a.txt").write_text("aaa")
    output = str(Path(tmp_manifest_dir) / "backup.tar.gz")
    result = create_archive(tmp_manifest_dir, output)
    assert result == output


def test_create_archive_skips_directories(tmp_manifest_dir):
    Path(tmp_manifest_dir, "a.txt").write_text("aaa")
    Path(tmp_manifest_dir, "subdir").mkdir(parents=True)
    Path(tmp_manifest_dir, "subdir", "b.txt").write_text("bbb")
    output = str(Path(tmp_manifest_dir) / "backup.tar.gz")
    result = create_archive(tmp_manifest_dir, output)
    assert result == output
    with tarfile.open(output) as tar:
        names = [m.name for m in tar.getmembers() if m.isfile()]
    assert names == ["a.txt"]


# ---------------------------------------------------------------------------
# encrypt_archive
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not openssl_available, reason="openssl not installed")
def test_encrypt_archive_round_trip(tmp_manifest_dir):
    plain = str(Path(tmp_manifest_dir) / "test.tar.gz")
    Path(plain).write_text("fake-tar-content")
    enc = plain + ".enc"

    encrypt_archive(plain, "test-pass")
    assert Path(enc).exists()
    assert not Path(plain).exists()

    dec = str(Path(tmp_manifest_dir) / "decrypted.tar.gz")
    result = subprocess.run(  # noqa: S603 — test fixture
        [  # noqa: S607
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "600000",
            "-in",
            enc,
            "-out",
            dec,
            "-pass",
            "pass:test-pass",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0
    assert Path(dec).read_text() == "fake-tar-content"


def test_encrypt_archive_missing_openssl_exits(tmp_manifest_dir, capsys):
    plain = str(Path(tmp_manifest_dir) / "a.tar.gz")
    Path(plain).write_text("x")
    with (
        patch("scripts.backup.shutil.which", return_value=None),
        pytest.raises(SystemExit),
    ):
        encrypt_archive(plain, "pass")
    assert "openssl not found" in capsys.readouterr().out


def test_encrypt_archive_openssl_failure_exits(tmp_manifest_dir, capsys):
    plain = str(Path(tmp_manifest_dir) / "a.tar.gz")
    Path(plain).write_text("x")
    proc = MagicMock()
    proc.returncode = 1
    proc.stderr = "Encryption error"
    with (
        patch("scripts.backup.shutil.which", return_value="/usr/bin/openssl"),
        patch("scripts.backup.subprocess.run", return_value=proc),
        pytest.raises(SystemExit),
    ):
        encrypt_archive(plain, "pass")
    assert "Encryption failed: Encryption error" in capsys.readouterr().out


def test_encrypt_archive_deletes_plaintext_on_success(tmp_manifest_dir):
    plain = str(Path(tmp_manifest_dir) / "a.tar.gz")
    Path(plain).write_text("x")
    proc = MagicMock()
    proc.returncode = 0
    with (
        patch("scripts.backup.shutil.which", return_value="/usr/bin/openssl"),
        patch("scripts.backup.subprocess.run", return_value=proc) as mock_run,
    ):
        encrypt_archive(plain, "pass")
    assert not Path(plain).exists()
    args = mock_run.call_args.args[0]
    assert args[0] == "openssl"
    assert plain in args
    assert plain + ".enc" in args
    assert "pass:pass" in args


# ---------------------------------------------------------------------------
# get_org_id
# ---------------------------------------------------------------------------


def test_get_org_id_uses_psql_output():
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "org-abc-123\n"
    with patch("scripts.backup.subprocess.run", return_value=proc) as mock_run:
        assert get_org_id("postgresql://u:p@h/db") == "org-abc-123"
    mock_run.assert_called_once()


def test_get_org_id_falls_back_when_psql_fails():
    proc = MagicMock()
    proc.returncode = 1
    proc.stdout = ""
    with patch("scripts.backup.subprocess.run", return_value=proc):
        result = get_org_id("postgresql://u:p@h/db")
    assert len(result) == 8
    assert all(c in "0123456789abcdef" for c in result)


def test_get_org_id_falls_back_on_exception():
    with patch("scripts.backup.subprocess.run", side_effect=OSError("psql not found")):
        result = get_org_id("postgresql://u:p@h/db")
    assert len(result) == 8
    assert all(c in "0123456789abcdef" for c in result)


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def test_parse_args_defaults(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["backup.py"])
    args = parse_args()
    assert args.output is None
    assert args.passphrase is None
    assert args.db_url is None
    assert args.pg_dump == "pg_dump"
    assert args.min_disk_gb == 1


def test_parse_args_overrides(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backup.py",
            "-o",
            "out.tar.gz.enc",
            "-p",
            "secret",
            "--db-url",
            "url",
            "--pg-dump",
            "/bin/pg_dump",
            "--min-disk-gb",
            "5",
        ],
    )
    args = parse_args()
    assert args.output == "out.tar.gz.enc"
    assert args.passphrase == "secret"
    assert args.db_url == "url"
    assert args.pg_dump == "/bin/pg_dump"
    assert args.min_disk_gb == 5


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


async def test_main_full_flow(tmp_manifest_dir, capsys):
    output = str(Path(tmp_manifest_dir) / "modulo-backup.tar.gz.enc")
    Path(output).write_text("enc")  # noqa: ASYNC240 — trivial 1-byte test setup, not a blocking risk
    ns = MagicMock()
    ns.output = output
    ns.passphrase = "pass"
    ns.db_url = None
    ns.pg_dump = "pg_dump"
    ns.min_disk_gb = 1
    tar_path = str(Path(tmp_manifest_dir) / "x.tar.gz")

    with (
        patch("scripts.backup.parse_args", return_value=ns),
        patch("scripts.backup.get_db_url", return_value="postgresql://u:p@h/db"),
        patch("scripts.backup.check_disk_space") as mock_disk,
        patch("scripts.backup.get_org_id", return_value="org123"),
        patch("scripts.backup.run_pg_dump", new=AsyncMock()) as mock_dump,
        patch("scripts.backup.collect_secrets", return_value=["secrets.env"]) as mock_secrets,
        patch("scripts.backup.write_checksums", return_value="checksums.sha256") as mock_cs,
        patch("scripts.backup.create_archive", return_value=tar_path) as mock_arc,
        patch("scripts.backup.encrypt_archive") as mock_enc,
    ):
        await main()

    mock_disk.assert_called_once()
    mock_dump.assert_awaited_once()
    mock_secrets.assert_called_once()
    mock_cs.assert_called_once()
    mock_arc.assert_called_once()
    mock_enc.assert_called_once_with(tar_path, "pass")
    out = capsys.readouterr().out
    assert "Starting backup (org=org123" in out
    assert "Backup complete:" in out


async def test_main_exits_on_empty_passphrase(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["backup.py"])
    with (
        patch("scripts.backup.resolve_passphrase", return_value=""),
        pytest.raises(SystemExit),
    ):
        await main()
    assert "passphrase cannot be empty" in capsys.readouterr().out


async def test_main_normalizes_output_without_enc_suffix(tmp_manifest_dir, capsys):
    raw_output = str(Path(tmp_manifest_dir) / "modulo-backup.tar.gz")
    enc_output = raw_output + ".enc"
    Path(enc_output).write_text("enc")  # noqa: ASYNC240 — trivial 1-byte test setup
    ns = MagicMock()
    ns.output = raw_output
    ns.passphrase = "pass"
    ns.db_url = None
    ns.pg_dump = "pg_dump"
    ns.min_disk_gb = 1

    with (
        patch("scripts.backup.parse_args", return_value=ns),
        patch("scripts.backup.get_db_url", return_value="postgresql://u:p@h/db"),
        patch("scripts.backup.check_disk_space"),
        patch("scripts.backup.get_org_id", return_value="org123"),
        patch("scripts.backup.run_pg_dump", new=AsyncMock()),
        patch("scripts.backup.collect_secrets", return_value=["secrets.env"]),
        patch("scripts.backup.write_checksums", return_value="checksums.sha256"),
        patch("scripts.backup.create_archive", return_value=raw_output) as mock_arc,
        patch("scripts.backup.encrypt_archive") as mock_enc,
    ):
        await main()

    # The encrypted archive must land exactly at the user-requested path, so
    # create_archive receives the .enc-normalised output and getsize reads the
    # encrypted file that encrypt_archive actually produces.
    assert mock_arc.call_args.args[1] == enc_output
    mock_enc.assert_called_once_with(raw_output, "pass")
    assert "Backup complete:" in capsys.readouterr().out


async def test_main_keeps_enc_suffix_output_unchanged(tmp_manifest_dir, capsys):
    output = str(Path(tmp_manifest_dir) / "modulo-backup.tar.gz.enc")
    Path(output).write_text("enc")  # noqa: ASYNC240 — trivial 1-byte test setup, not a blocking risk
    ns = MagicMock()
    ns.output = output
    ns.passphrase = "pass"
    ns.db_url = None
    ns.pg_dump = "pg_dump"
    ns.min_disk_gb = 1
    tar_path = str(Path(tmp_manifest_dir) / "x.tar.gz")

    with (
        patch("scripts.backup.parse_args", return_value=ns),
        patch("scripts.backup.get_db_url", return_value="postgresql://u:p@h/db"),
        patch("scripts.backup.check_disk_space"),
        patch("scripts.backup.get_org_id", return_value="org123"),
        patch("scripts.backup.run_pg_dump", new=AsyncMock()),
        patch("scripts.backup.collect_secrets", return_value=["secrets.env"]),
        patch("scripts.backup.write_checksums", return_value="checksums.sha256"),
        patch("scripts.backup.create_archive", return_value=tar_path) as mock_arc,
        patch("scripts.backup.encrypt_archive") as mock_enc,
    ):
        await main()

    assert mock_arc.call_args.args[1] == output
    mock_enc.assert_called_once_with(tar_path, "pass")
    assert "Backup complete:" in capsys.readouterr().out
