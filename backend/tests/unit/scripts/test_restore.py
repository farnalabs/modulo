"""Unit tests for restore.py — dry-run, decryption, extraction, verification."""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
from pathlib import Path

import pytest
from scripts.restore import (
    extract_archive,
    get_db_url,
    read_checksums,
    resolve_passphrase,
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
    content_dir = os.path.join(tmp_dir, "content")
    os.makedirs(content_dir, exist_ok=True)
    Path(os.path.join(content_dir, "modulo.pgdump")).write_text("fake-dump-content")
    Path(os.path.join(content_dir, "secrets.env")).write_text("FERNET_KEY=test\n")
    Path(os.path.join(content_dir, "manifest.json")).write_text('{"tool": "modulo-backup", "version": "1"}')
    # write checksums
    from scripts.backup import hash_file as bhash

    with open(os.path.join(content_dir, "checksums.sha256"), "w") as f:
        for name in ("modulo.pgdump", "secrets.env", "manifest.json"):
            h = bhash(os.path.join(content_dir, name))
            f.write(f"{h}  {name}\n")

    archive_path = os.path.join(tmp_dir, "backup.tar.gz")
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


# ---------------------------------------------------------------------------
# decrypt_archive
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not openssl_available, reason="openssl not installed")
def test_decrypt_archive_missing_input(tmp_dir, monkeypatch):
    monkeypatch.setenv("MODULO_BACKUP_PASSPHRASE", "test")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from scripts.restore import decrypt_archive as da
    from scripts.restore import resolve_passphrase as rp

    passphrase = rp(None)
    with pytest.raises(SystemExit):
        da("/nonexistent", passphrase, os.path.join(tmp_dir, "out.tar.gz"))


# ---------------------------------------------------------------------------
# extract_archive
# ---------------------------------------------------------------------------


def test_extract_archive(tmp_dir, sample_archive):
    archive_path, _ = sample_archive
    extract_dir = os.path.join(tmp_dir, "extracted")
    os.makedirs(extract_dir, exist_ok=True)
    files = extract_archive(archive_path, extract_dir)
    assert "modulo.pgdump" in files
    assert "secrets.env" in files
    assert "manifest.json" in files
    assert os.path.exists(files["modulo.pgdump"])


# ---------------------------------------------------------------------------
# read_checksums
# ---------------------------------------------------------------------------


def test_read_checksums(tmp_dir, sample_archive):
    _, content_dir = sample_archive
    checksums = read_checksums(content_dir)
    assert len(checksums) == 3
    assert "modulo.pgdump" in checksums
    for name, h in checksums.items():
        assert len(h) == 64  # SHA-256 hex


def test_read_checksums_missing(tmp_dir):
    assert read_checksums(tmp_dir) == {}


# ---------------------------------------------------------------------------
# verify_hashes
# ---------------------------------------------------------------------------


def test_verify_hashes_ok(tmp_dir, sample_archive):
    _, content_dir = sample_archive
    files = {
        "modulo.pgdump": os.path.join(content_dir, "modulo.pgdump"),
        "secrets.env": os.path.join(content_dir, "secrets.env"),
        "manifest.json": os.path.join(content_dir, "manifest.json"),
    }
    assert verify_hashes(content_dir, files) is True


def test_verify_hashes_fails_on_corrupted(tmp_dir, sample_archive):
    _, content_dir = sample_archive
    pg_path = os.path.join(content_dir, "modulo.pgdump")
    # corrupt the file
    Path(pg_path).write_text("tampered content")
    files = {"modulo.pgdump": pg_path}
    assert verify_hashes(content_dir, files) is False


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
