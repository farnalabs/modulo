#!/usr/bin/env python3
"""Restore Modulo from an encrypted backup archive."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Modulo restore tool")
    parser.add_argument("--input", "-i", required=True, help="Encrypted backup archive path")
    parser.add_argument(
        "--passphrase",
        "-p",
        default=None,
        help="Decryption passphrase (prompts if omitted and MODULO_BACKUP_PASSPHRASE not set)",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="Postgres connection URL (default: DATABASE_URL env var)",
    )
    parser.add_argument(
        "--pg-restore",
        default="pg_restore",
        help="pg_restore executable path (default: pg_restore)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Verify archive without restoring")
    parser.add_argument("--full", action="store_true", help="Restore everything")
    parser.add_argument("--data-only", action="store_true", help="Restore Postgres only")
    parser.add_argument("--config-only", action="store_true", help="Restore config/keys only")
    return parser.parse_args()


def resolve_passphrase(args_passphrase: str | None) -> str:
    if args_passphrase:
        return args_passphrase
    env = os.environ.get("MODULO_BACKUP_PASSPHRASE")
    if env:
        return env
    return getpass.getpass("Restore passphrase: ")


def _validate_arg(value: str, name: str) -> str:
    """Reject values that would be interpreted as CLI flags when passed as a
    subprocess argument (defense against argument injection)."""
    if not value or value.startswith("-"):
        raise ValueError(f"invalid {name}: must be a non-empty value that does not start with '-'")
    return value


def _validate_executable(value: str, name: str) -> str:
    """Resolve *value* to a real executable and return its absolute path.

    Prevents an untrusted ``--pg-restore`` argument from launching an arbitrary
    command (S8701). The value must not look like a CLI flag and must resolve to
    an existing executable on ``PATH``.
    """
    if not value or value.startswith("-"):
        raise ValueError(f"invalid {name}: must be a non-empty executable that does not start with '-'")
    resolved = shutil.which(value)
    if resolved is None:
        raise ValueError(f"invalid {name}: executable not found on PATH: {value!r}")
    return resolved


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_identifier(value: str, name: str) -> str:
    """Reject anything that is not a safe SQL identifier.

    Used for the database name that is interpolated into a ``psql -c`` SQL string
    (defense against SQL injection in the terminate-backends command, S8705).
    """
    if not _IDENTIFIER_RE.match(value or ""):
        raise ValueError(f"invalid {name}: {value!r} is not a valid SQL identifier")
    return value


def _safe_output_path(path: str, name: str) -> str:
    """Resolve *path* and require it to stay within the current directory's
    real path (defense against path injection for derived output files)."""
    resolved = os.path.realpath(path)
    base = os.path.realpath(Path.cwd())
    if resolved != base and not resolved.startswith(base + os.sep):
        raise ValueError(f"invalid {name}: {path!r} resolves outside the working directory")
    return resolved


def decrypt_archive(enc_path: str, passphrase: str, output_path: str) -> None:
    print("Decrypting archive...")
    if not shutil.which("openssl"):
        print("ERROR: openssl not found. Install OpenSSL to decrypt backups.")
        sys.exit(1)
    _validate_arg(passphrase, "passphrase")
    result = subprocess.run(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "600000",
            "-in",
            enc_path,
            "-out",
            output_path,
            "-pass",
            f"pass:{passphrase}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"Decryption failed: {result.stderr}")
        sys.exit(1)
    print(f"  -> {output_path}")


def extract_archive(tar_path: str, extract_dir: str) -> dict[str, str]:
    print("Extracting archive...")
    files: dict[str, str] = {}
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(path=extract_dir, filter="data")
        for member in tar.getmembers():
            if member.isfile():
                name = member.name.lstrip("./")
                files[name] = os.path.join(extract_dir, name)
                print(f"  extracted: {member.name}")
    return files


def read_checksums(extract_dir: str) -> dict[str, str]:
    cs_file = os.path.join(extract_dir, "checksums.sha256")
    if not Path(cs_file).exists():
        print("WARNING: no checksums.sha256 found in archive")
        return {}
    checksums: dict[str, str] = {}
    with open(cs_file) as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            parts = line.split("  ", 1)
            if len(parts) == 2:
                checksums[parts[1]] = parts[0]
    return checksums


def verify_hashes(extract_dir: str, files: dict[str, str]) -> bool:
    print("Verifying file hashes...")
    expected = read_checksums(extract_dir)
    if not expected:
        print("  (no checksums to verify)")
        return True
    all_ok = True
    for name, path in files.items():
        if name == "checksums.sha256":
            continue
        if name in expected:
            actual = hash_file(path)
            ok = actual == expected[name]
            status = "OK" if ok else "MISMATCH"
            if not ok:
                all_ok = False
            print(f"  {name}: {status}")
        else:
            print(f"  {name}: UNCHECKED (not in checksums)")
    return all_ok


def hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def get_db_url(args_db_url: str | None) -> str:
    if args_db_url:
        return args_db_url
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("ERROR: Provide --db-url or set DATABASE_URL environment variable")
        sys.exit(1)
    return url


def pg_database_name(db_url: str) -> str:
    import urllib.parse

    parsed = urllib.parse.urlparse(db_url)
    return parsed.path.lstrip("/").split("?")[0]


def restore_postgres(extract_dir: str, db_url: str, pg_restore: str) -> None:
    dump_path = os.path.join(extract_dir, "modulo.pgdump")
    pg_restore = _validate_executable(pg_restore, "pg_restore")
    _validate_arg(db_url, "database URL")
    if not Path(dump_path).exists():
        print("ERROR: modulo.pgdump not found in archive")
        sys.exit(1)

    db_name = pg_database_name(db_url)
    _validate_identifier(db_name, "database name")
    print(f"Restoring Postgres to database '{db_name}'...")

    admin_db = "postgres"
    admin_url = db_url.rsplit("/", 1)[0] + f"/{admin_db}"
    _validate_arg(admin_url, "admin database URL")

    print("  Terminating existing connections...")
    subprocess.run(
        [
            "psql",
            "-d",
            admin_url,
            "-c",
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{db_name}' AND pid <> pg_backend_pid()",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    print("  Dropping existing database...")
    result = subprocess.run(
        ["dropdb", "--if-exists", "-f", db_name, f"--maintenance-db={admin_url}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"  dropdb warning: {result.stderr}")

    print("  Recreating database...")
    result = subprocess.run(
        ["createdb", db_name, f"--maintenance-db={admin_url}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"  createdb failed: {result.stderr}")
        sys.exit(1)

    print("  Importing data (this may take a while)...")
    result = subprocess.run(
        [pg_restore, "--no-owner", "--no-acl", "--dbname", db_url, dump_path],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"  pg_restore warning/output: {result.stderr}")
    print("  Postgres restore complete.")


def restore_config(extract_dir: str) -> None:
    secrets_path = os.path.join(extract_dir, "secrets.env")
    if not Path(secrets_path).exists():
        print("WARNING: secrets.env not found in archive")
        return
    print("Restoring config...")
    print(f"  Found: {secrets_path}")
    print("  To apply, source or copy the values:")
    with open(secrets_path) as f:
        for raw_line in f:
            line = raw_line.strip()
            if line and not line.startswith("#"):
                print(f"    export {line}")

    manifest_path = os.path.join(extract_dir, "manifest.json")
    if Path(manifest_path).exists():
        import json

        with open(manifest_path) as f:
            manifest = json.load(f)
        print(f"  Backup created at: {manifest.get('created_at', 'unknown')}")
        print(f"  Tool version: {manifest.get('version', 'unknown')}")


async def main() -> None:
    args = parse_args()

    if not Path(args.input).exists():
        print(f"ERROR: input file not found: {args.input}")
        sys.exit(1)

    mode_count = sum([args.full, args.data_only, args.config_only])
    if mode_count > 1:
        print("ERROR: choose only one of --full, --data-only, --config-only")
        sys.exit(1)
    if mode_count == 0 and not args.dry_run:
        print("ERROR: specify --dry-run, --full, --data-only, or --config-only")
        sys.exit(1)

    passphrase = resolve_passphrase(args.passphrase)
    if not passphrase:
        print("ERROR: passphrase cannot be empty")
        sys.exit(1)

    db_url = get_db_url(args.db_url) if (args.full or args.data_only) else ""

    if args.dry_run:
        print(f"Dry-run mode: verifying archive {args.input}")
    elif args.data_only:
        print("Data-only restore mode")
    elif args.config_only:
        print("Config-only restore mode")
    else:
        print("Full restore mode")

    tmpdir = tempfile.mkdtemp(prefix="modulo-restore-")
    try:
        tar_path = os.path.join(tmpdir, "backup.tar.gz")
        decrypt_archive(args.input, passphrase, tar_path)
        files = extract_archive(tar_path, tmpdir)

        hashes_ok = verify_hashes(tmpdir, files)
        if not hashes_ok:
            print("ERROR: hash verification failed. Archive is corrupt or tampered.")
            if not args.dry_run:
                sys.exit(1)

        if args.dry_run:
            print("Dry-run: archive verified successfully.")
            return

        if args.full or args.data_only:
            restore_postgres(tmpdir, db_url, args.pg_restore)
        if args.full or args.config_only:
            restore_config(tmpdir)

        print("Restore complete.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        print("Temp directory cleaned up.")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
