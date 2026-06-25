"""Unit tests for backup.py — archive creation, encryption, metadata."""

from __future__ import annotations

import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import shutil

from scripts.backup import (
    collect_secrets,
    create_archive,
    encrypt_archive,
    get_db_url,
    hash_file,
    resolve_passphrase,
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
# collect_secrets
# ---------------------------------------------------------------------------


def test_collect_secrets_creates_env_file(tmp_manifest_dir, monkeypatch):
    monkeypatch.setenv("FERNET_KEY", "test-fernet-key")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")
    files = collect_secrets(tmp_manifest_dir)

    secrets_path = os.path.join(tmp_manifest_dir, "secrets.env")
    assert secrets_path in files
    assert os.path.exists(secrets_path)

    with open(secrets_path) as f:
        content = f.read()
    assert "FERNET_KEY=test-fernet-key" in content
    assert "SECRET_KEY=test-secret-key" in content


def test_collect_secrets_creates_manifest(tmp_manifest_dir):
    files = collect_secrets(tmp_manifest_dir)
    manifest_path = os.path.join(tmp_manifest_dir, "manifest.json")
    assert manifest_path in files
    assert os.path.exists(manifest_path)

    with open(manifest_path) as f:
        manifest = json.load(f)
    assert manifest["tool"] == "modulo-backup"
    assert manifest["version"] == "1"
    assert "created_at" in manifest


# ---------------------------------------------------------------------------
# hash_file, write_checksums
# ---------------------------------------------------------------------------


def test_hash_file_consistent(tmp_manifest_dir):
    path = os.path.join(tmp_manifest_dir, "test.txt")
    Path(path).write_text("hello world")
    h1 = hash_file(path)
    h2 = hash_file(path)
    assert h1 == h2


def test_write_checksums(tmp_manifest_dir):
    a = os.path.join(tmp_manifest_dir, "a.dat")
    b = os.path.join(tmp_manifest_dir, "b.dat")
    Path(a).write_text("aaa")
    Path(b).write_text("bbb")
    cs_path = write_checksums(tmp_manifest_dir, [a, b])
    assert os.path.exists(cs_path)

    with open(cs_path) as f:
        lines = f.read().strip().split("\n")
    assert len(lines) == 2
    for line in lines:
        assert "  " in line
        h, name = line.split("  ", 1)
        assert len(h) == 64  # SHA-256 hex
        assert name in ("a.dat", "b.dat")


# ---------------------------------------------------------------------------
# create_archive
# ---------------------------------------------------------------------------


def test_create_archive_packs_files(tmp_manifest_dir):
    Path(os.path.join(tmp_manifest_dir, "a.txt")).write_text("aaa")
    Path(os.path.join(tmp_manifest_dir, "b.txt")).write_text("bbb")
    output = os.path.join(tmp_manifest_dir, "backup.tar.gz")
    result = create_archive(tmp_manifest_dir, output)
    assert result == output
    assert os.path.exists(output)
    assert tarfile.is_tarfile(output)


# ---------------------------------------------------------------------------
# encrypt_archive / decrypt round-trip
# ---------------------------------------------------------------------------


def test_encrypt_archive_round_trip(tmp_manifest_dir):
    if not openssl_available:
        pytest.skip("openssl not installed")
    plain = os.path.join(tmp_manifest_dir, "test.tar.gz")
    Path(plain).write_text("fake-tar-content")
    enc = plain + ".enc"

    encrypt_archive(plain, "test-pass")
    assert os.path.exists(enc)
    assert not os.path.exists(plain)

    dec = os.path.join(tmp_manifest_dir, "decrypted.tar.gz")
    result = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "600000",
         "-in", enc, "-out", dec, "-pass", "pass:test-pass"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert Path(dec).read_text() == "fake-tar-content"
