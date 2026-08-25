#!/usr/bin/env python3
"""Backup Modulo Postgres schema+data, secrets, and config into an encrypted archive."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from datetime import UTC, datetime
from typing import NamedTuple


class BackupManifest(NamedTuple):
    org_id: str
    timestamp: str
    files: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Modulo backup tool")
    parser.add_argument("--output", "-o", default=None, help="Output archive path")
    parser.add_argument(
        "--passphrase",
        "-p",
        default=None,
        help="Encryption passphrase (prompts if omitted and MODULO_BACKUP_PASSPHRASE not set)",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="Postgres connection URL (default: DATABASE_URL env var)",
    )
    parser.add_argument(
        "--pg-dump",
        default="pg_dump",
        help="pg_dump executable path (default: pg_dump)",
    )
    parser.add_argument(
        "--min-disk-gb",
        type=int,
        default=1,
        help="Minimum free disk space in GB (default: 1)",
    )
    return parser.parse_args()


def resolve_passphrase(args_passphrase: str | None) -> str:
    if args_passphrase:
        return args_passphrase
    env = os.environ.get("MODULO_BACKUP_PASSPHRASE")
    if env:
        return env
    return getpass.getpass("Backup passphrase: ")


def _validate_arg(value: str, name: str) -> str:
    """Reject values that would be interpreted as CLI flags when passed as a
    subprocess argument (defense against argument injection)."""
    if not value or value.startswith("-"):
        raise ValueError(f"invalid {name}: must be a non-empty value that does not start with '-'")
    return value


def _validate_executable(value: str, name: str) -> str:
    """Resolve *value* to a real executable and return its absolute path.

    Prevents an untrusted ``--pg-dump`` argument from launching an arbitrary
    command (S8701). The value must not look like a CLI flag and must resolve to
    an existing executable on ``PATH``.
    """
    if not value or value.startswith("-"):
        raise ValueError(f"invalid {name}: must be a non-empty executable that does not start with '-'")
    resolved = shutil.which(value)
    if resolved is None:
        raise ValueError(f"invalid {name}: executable not found on PATH: {value!r}")
    return resolved


def check_disk_space(path: str, min_gb: int) -> None:
    usage = shutil.disk_usage(path)
    free_gb = usage.free / (1024**3)
    print(f"Disk space: {free_gb:.1f} GB free at {path}")
    if free_gb < min_gb:
        print(f"ERROR: Insufficient disk space ({free_gb:.1f} GB < {min_gb} GB required)")
        sys.exit(1)


def get_db_url(args_db_url: str | None) -> str:
    if args_db_url:
        return args_db_url
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ERROR: Provide --db-url or set DATABASE_URL environment variable")
        sys.exit(1)
    return url


async def run_pg_dump(db_url: str, pg_dump: str, output_path: str) -> None:
    print("Dumping Postgres schema+data...")
    proc = await asyncio.create_subprocess_exec(
        pg_dump,
        "--no-owner",
        "--no-acl",
        "--format=custom",
        "--file",
        output_path,
        db_url,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        print(f"pg_dump failed: {stderr.decode(errors='replace')}")
        sys.exit(1)
    print(f"  -> {output_path}")


def collect_secrets(manifest_dir: str) -> list[str]:
    print("Collecting secrets...")
    files: list[str] = []
    secrets_path = os.path.join(manifest_dir, "secrets.env")
    keys = ["FERNET_KEY", "SECRET_KEY", "DATABASE_URL", "MODULO_PUBLIC_URL", "REDIS_URL"]
    with open(secrets_path, "w") as f:
        for key in keys:
            val = os.environ.get(key, "")
            f.write(f"{key}={val}\n")
    files.append(secrets_path)
    print(f"  -> {secrets_path}")

    manifest = {
        "tool": "modulo-backup",
        "version": "1",
        "created_at": datetime.now(UTC).isoformat(),
    }
    manifest_path = os.path.join(manifest_dir, "manifest.json")
    import json

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    files.append(manifest_path)
    print(f"  -> {manifest_path}")

    return files


def hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_checksums(manifest_dir: str, files: list[str]) -> str:
    checksums: dict[str, str] = {}
    for f in files:
        rel = os.path.basename(f)
        checksums[rel] = hash_file(f)
    cs_path = os.path.join(manifest_dir, "checksums.sha256")
    with open(cs_path, "w") as f:
        f.writelines(f"{h}  {name}\n" for name, h in sorted(checksums.items()))
    return cs_path


def create_archive(manifest_dir: str, output_path: str) -> str:
    print("Creating archive...")
    tar_path = output_path.removesuffix(".enc")
    with tarfile.open(tar_path, "w:gz") as tar:
        for entry in os.listdir(manifest_dir):
            full = os.path.join(manifest_dir, entry)
            if os.path.isfile(full):
                tar.add(full, arcname=entry)
    print(f"  -> {tar_path}")
    return tar_path


def encrypt_archive(tar_path: str, passphrase: str) -> None:
    enc_path = tar_path + ".enc"
    print("Encrypting archive...")
    if not shutil.which("openssl"):
        print("ERROR: openssl not found. Install OpenSSL to encrypt backups.")
        sys.exit(1)
    _validate_arg(passphrase, "passphrase")
    result = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-256-cbc",
            "-salt",
            "-pbkdf2",
            "-iter",
            "600000",
            "-in",
            tar_path,
            "-out",
            enc_path,
            "-pass",
            f"pass:{passphrase}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"Encryption failed: {result.stderr}")
        sys.exit(1)
    print(f"  -> {enc_path}")
    os.unlink(tar_path)


def get_org_id(db_url: str) -> str:
    try:
        _validate_arg(db_url, "database URL")
        result = subprocess.run(
            ["psql", "-d", db_url, "-t", "-A", "-c", "SELECT id FROM organisations ORDER BY created_at LIMIT 1"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:  # noqa: S110
        pass
    return uuid.uuid4().hex[:8]


async def main() -> None:
    args = parse_args()
    passphrase = resolve_passphrase(args.passphrase)
    if not passphrase:
        print("ERROR: passphrase cannot be empty")
        sys.exit(1)

    db_url = get_db_url(args.db_url)

    check_disk_space(os.path.dirname(args.output or "."), args.min_disk_gb)

    pg_dump = _validate_executable(args.pg_dump, "pg_dump")

    org_id = get_org_id(db_url)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or f"modulo-backup-{org_id}-{timestamp}.tar.gz.enc"
    if not output.endswith(".enc"):
        output += ".enc"

    print(f"Starting backup (org={org_id}, timestamp={timestamp})")
    print(f"Output: {output}")

    tmpdir = tempfile.mkdtemp(prefix="modulo-backup-")
    try:
        dump_path = os.path.join(tmpdir, "modulo.pgdump")
        await run_pg_dump(db_url, pg_dump, dump_path)

        secret_files = collect_secrets(tmpdir)
        all_files = [dump_path, *secret_files]

        cs_path = write_checksums(tmpdir, all_files)
        all_files.append(cs_path)

        tar_path = create_archive(tmpdir, output)
        encrypt_archive(tar_path, passphrase)

        final_size = os.path.getsize(output)
        print(f"Backup complete: {output} ({final_size / 1024 / 1024:.1f} MB)")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
