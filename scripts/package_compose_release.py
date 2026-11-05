#!/usr/bin/env python3
"""Package the self-hosted production compose stack into a release asset bundle (FAR-2345).

Bundles:
- docker-compose.prod.yml (with hard-pinned immutable tag ghcr.io/farnalabs/modulo:<TAG>)
- docker-compose.yml (identical copy for direct `docker compose up -d`)
- .env.example (working evaluation environment configuration)
- quickstart.md (getting started instructions)
- SHA256SUMS (checksums over the packaged files)

Produces:
- modulo-<VERSION>-compose.tar.gz
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import sys
import tarfile
from pathlib import Path

import yaml

_GIT_SHA_PATTERN = re.compile(r"^[\da-fA-F]{7,40}$", re.ASCII)
_SEMVER_PATTERN = re.compile(r"^v?\d+\.\d+\.\d+(-[a-zA-Z\d.]+)?$", re.ASCII)
_COMPOSE_FILENAME = "docker-compose.prod.yml"


def validate_release_tag(tag: str) -> str:
    """Validate that the container image tag is immutable, non-empty, and not a git SHA.

    Raises ValueError if invalid.
    """
    clean_tag = tag.strip()
    if not clean_tag:
        raise ValueError("Release tag cannot be empty")
    if clean_tag.lower() in {"latest", ":latest"}:
        raise ValueError("Release tag cannot be mutable ':latest'")
    if _GIT_SHA_PATTERN.match(clean_tag):
        raise ValueError(f"Release tag cannot be a git SHA: '{clean_tag}'")
    return clean_tag


def transform_compose_content(content: str, tag: str) -> str:
    """Transform docker-compose.prod.yml for a standalone evaluator release bundle.

    1. Hard-pins `image: ghcr.io/farnalabs/modulo:<TAG>` on the modulo service.
    2. Removes the repo-internal watchdog service (which requires local source build ../watchdog).
    3. Validates the resulting YAML structure.
    """
    valid_tag = validate_release_tag(tag)

    # 1. Hard-pin image tag on the modulo service
    image_pattern = re.compile(
        r"(^\s*image:\s*ghcr\.io/farnalabs/modulo:)[^\r\n]+",
        re.MULTILINE,
    )
    if not image_pattern.search(content):
        raise ValueError("Could not find 'image: ghcr.io/farnalabs/modulo:...' in compose content")

    transformed = image_pattern.sub(rf"\1{valid_tag}", content, count=1)

    # 2. Remove the repo-internal watchdog service block if present.
    #
    # Text surgery, not a YAML round-trip: this file carries the reasoning for
    # every setting in its comments, and re-dumping the parsed document would
    # throw all of it away - the comments are most of the value for an operator
    # reading the bundle. The block runs from its explanatory comment to the
    # next top-level key (a line starting in column 0). The result is
    # re-parsed and asserted below, so a mis-cut fails loudly instead of
    # shipping a subtly broken compose file.
    watchdog_comments = re.compile(r"(?m)(?:^[ \t]*#[^\n]*\n)+(?=^[ \t]*watchdog:)")
    watchdog_service = re.compile(r"(?m)^[ \t]*watchdog:\n(?:^[ \t]+.*(?:\n|$)|^\n)*")
    transformed = watchdog_comments.sub("", transformed, count=1)
    transformed = watchdog_service.sub("", transformed, count=1)

    # 3. Validate parsed YAML
    data = yaml.safe_load(transformed)
    if not isinstance(data, dict):
        raise ValueError("Transformed compose content did not parse as a YAML mapping")

    services = data.get("services", {})
    if "postgres" not in services:
        raise ValueError("Transformed compose is missing 'postgres' service")
    if "redis" not in services:
        raise ValueError("Transformed compose is missing 'redis' service")
    if "modulo" not in services:
        raise ValueError("Transformed compose is missing 'modulo' service")

    modulo_image = services["modulo"].get("image", "")
    expected_image = f"ghcr.io/farnalabs/modulo:{valid_tag}"
    if modulo_image != expected_image:
        raise ValueError(f"Expected modulo image '{expected_image}', got '{modulo_image}'")

    if "watchdog" in services:
        raise ValueError("Watchdog service was not removed from standalone release compose")

    volumes = data.get("volumes", {})
    if "postgres_data" not in volumes or "redis_data" not in volumes:
        raise ValueError("Transformed compose is missing required volumes")

    return transformed


def generate_quickstart_md(version: str, tag: str) -> str:
    """Generate quickstart instructions for the compose release bundle."""
    return f"""# Modulo Self-Hosted Production Quickstart (v{version})

This package contains everything required to run the Modulo all-in-one
production stack on your own infrastructure with Docker Compose.

**Image pinned:** `ghcr.io/farnalabs/modulo:{tag}`

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) 24.0+
- [Docker Compose](https://docs.docker.com/compose/) v2.20+

## Quick Start (2 commands)

1. **Generate the environment file.** `init-env.sh` copies `.env.example` to
   `.env` and fills every REQUIRED secret with a freshly generated value:
   ```bash
   ./init-env.sh
   ```

2. **Start the stack:**
   ```bash
   docker compose up -d
   ```
   *(equivalently: `docker compose -f docker-compose.prod.yml up -d`)*

Then verify readiness:
```bash
curl -f http://localhost:80/healthz/ready
```
Once that returns 200, open [http://localhost](http://localhost).

> **Do not reuse these secrets for a real deployment without review.**
> `init-env.sh` generates unique random values on the machine that runs it, but
> a production stack should keep them in your own secret store and rotate them
> on the schedule your policy requires.

## Shipped Services

- **modulo** - all-in-one application: FastAPI backend, Vue 3 SPA served by
  nginx, and both SAQ workers (runs + the system worker that fires cron and
  polling triggers)
- **postgres** - PostgreSQL 16, persisted in the `postgres_data` volume
- **redis** - Redis 8, persisted in the `redis_data` volume

## Operations

- Follow logs: `docker compose logs -f modulo`
- Show status: `docker compose ps`
- Stop: `docker compose down`
- Stop and delete all data: `docker compose down -v`

## Integrity Verification

Every shipped file is listed in `SHA256SUMS`:
```bash
sha256sum --check SHA256SUMS
```
"""


#: Generated into every bundle. Deliberately carries no secret of its own: it
#: derives all five REQUIRED values on the machine that runs it, so a published
#: release asset never ships a shared signing key or database password.
_INIT_ENV_SH = r"""#!/bin/sh
# Generate .env for the Modulo production stack.
#
# Fills the five REQUIRED values the compose refuses to start without:
#   MODULO_DB_PASSWORD, SECRET_KEY, FERNET_KEY, SAQ_AUTH_USERNAME, SAQ_AUTH_PASSWORD
#
# Usage:
#   ./init-env.sh          create .env (refuses to overwrite an existing one)
#   ./init-env.sh --force  recreate .env from scratch
#
# No dependency beyond a POSIX shell: openssl is used when present, otherwise
# /dev/urandom. Note: `set -e` only - these generators normally exit non-zero
# with SIGPIPE once `head` has the bytes it wants, and `pipefail` would turn
# that into a spurious failure.
set -eu

if [ "${1:-}" != "--force" ] && [ -f .env ]; then
  echo "init-env: .env already exists; refusing to overwrite it (use --force to recreate)." >&2
  exit 1
fi

if [ ! -f .env.example ]; then
  echo "init-env: .env.example is missing - run this from the extracted bundle directory." >&2
  exit 1
fi

gen_b64() {
  # $1 = number of random bytes; prints standard base64 (no trailing newline).
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 "$1" | tr -d '\n'
  else
    head -c "$1" /dev/urandom | base64 | tr -d '\n'
  fi
}

gen_hex() {
  # $1 = number of hex characters; URL-safe so it can be embedded in a DB URL.
  tr -dc 'a-f0-9' < /dev/urandom | head -c "$1"
}

# MODULO_DB_PASSWORD is embedded in DATABASE_URL, so it must survive a URL
# unescaped: hex only.
DB_PASSWORD="$(gen_hex 48)"
# SECRET_KEY needs >= 32 bytes; 48 bytes of base64 satisfies Settings' length
# check without colliding with its blocked-placeholder list.
SECRET_KEY="$(gen_b64 48)"
# FERNET_KEY must decode to exactly 32 bytes (cryptography rejects anything
# else on first credential use). `openssl rand -base64 32` emits exactly that.
FERNET_KEY="$(gen_b64 32)"
SAQ_AUTH_PASSWORD="$(gen_b64 24)"

cp .env.example .env

# `|` delimiter: base64 can contain `/` and `+` but never `|`.
sed -i.bak \
  -e "s|^MODULO_DB_PASSWORD=.*|MODULO_DB_PASSWORD=${DB_PASSWORD}|" \
  -e "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET_KEY}|" \
  -e "s|^FERNET_KEY=.*|FERNET_KEY=${FERNET_KEY}|" \
  -e "s|^SAQ_AUTH_USERNAME=.*|SAQ_AUTH_USERNAME=admin|" \
  -e "s|^SAQ_AUTH_PASSWORD=.*|SAQ_AUTH_PASSWORD=${SAQ_AUTH_PASSWORD}|" \
  .env
rm -f .env.bak

missing=""
for name in MODULO_DB_PASSWORD SECRET_KEY FERNET_KEY SAQ_AUTH_USERNAME SAQ_AUTH_PASSWORD; do
  if ! grep -q "^${name}=..*" .env; then
    missing="${missing} ${name}"
  fi
done
if [ -n "$missing" ]; then
  echo "init-env: failed to populate REQUIRED variable(s):${missing}" >&2
  exit 1
fi

chmod 0600 .env
echo "init-env: wrote .env with freshly generated secrets (mode 0600)."
echo "Next: docker compose up -d"
"""


def _find_repo_root() -> Path:
    """Locate repository root by walking up from this script."""
    current = Path(__file__).resolve().parent
    while current != current.parent:
        if (current / "deploy" / "compose" / _COMPOSE_FILENAME).is_file():
            return current
        current = current.parent
    return Path.cwd()


def build_compose_release(
    *,
    version: str,
    tag: str | None = None,
    output_dir: Path | str = ".",
    compose_file: Path | str | None = None,
    env_file: Path | str | None = None,
) -> Path:
    """Assemble and package the compose release bundle into a tar.gz archive.

    Returns the path to the created tar.gz file.
    """
    clean_version = version.strip().lstrip("v")
    if not clean_version:
        raise ValueError("Version cannot be empty")
    if not _SEMVER_PATTERN.match(clean_version):
        raise ValueError(f"Version must be semver X.Y.Z; got '{clean_version}'")

    effective_tag = tag.strip() if tag else f"v{clean_version}"
    valid_tag = validate_release_tag(effective_tag)

    repo_root = _find_repo_root()

    # Locate source compose file
    src_compose = Path(compose_file) if compose_file else repo_root / "deploy" / "compose" / _COMPOSE_FILENAME
    if not src_compose.is_file():
        raise FileNotFoundError(f"Source compose file not found: {src_compose}")

    # Locate source env file
    if env_file:
        src_env = Path(env_file)
    else:
        candidate_env = repo_root / "deploy" / "compose" / ".env.example"
        src_env = candidate_env if candidate_env.is_file() else repo_root / "deploy" / "compose" / ".env.prod.example"

    if not src_env.is_file():
        raise FileNotFoundError(f"Source env example file not found: {src_env}")

    # Read and transform compose file
    compose_raw = src_compose.read_text(encoding="utf-8")
    compose_transformed = transform_compose_content(compose_raw, valid_tag)

    # Read env file
    env_content = src_env.read_text(encoding="utf-8")

    # Generate quickstart instructions
    quickstart_content = generate_quickstart_md(clean_version, valid_tag)

    # Prepare file bytes map for tarball
    files_to_pack: dict[str, bytes] = {
        _COMPOSE_FILENAME: compose_transformed.encode("utf-8"),
        "docker-compose.yml": compose_transformed.encode("utf-8"),
        ".env.example": env_content.encode("utf-8"),
        "quickstart.md": quickstart_content.encode("utf-8"),
        "init-env.sh": _INIT_ENV_SH.encode("utf-8"),
    }
    # `init-env.sh` is meant to be executed by the operator, so it ships 0755;
    # everything else is read-only data.
    executable = frozenset({"init-env.sh"})

    # Compute SHA256SUMS over the files
    sums_lines: list[str] = []
    for filename in sorted(files_to_pack.keys()):
        file_hash = hashlib.sha256(files_to_pack[filename]).hexdigest()
        sums_lines.append(f"{file_hash}  {filename}\n")
    sums_content = "".join(sums_lines).encode("utf-8")
    files_to_pack["SHA256SUMS"] = sums_content

    # Target output archive
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tarball_name = f"modulo-{clean_version}-compose.tar.gz"
    tarball_path = out_dir / tarball_name

    # Create deterministic tar.gz
    with tarfile.open(tarball_path, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        for filename in sorted(files_to_pack.keys()):
            data = files_to_pack[filename]
            tarinfo = tarfile.TarInfo(name=filename)
            tarinfo.size = len(data)
            tarinfo.mtime = 0
            tarinfo.mode = 0o755 if filename in executable else 0o644
            tarinfo.uid = 0
            tarinfo.gid = 0
            tarinfo.uname = ""
            tarinfo.gname = ""
            tar.addfile(tarinfo, io.BytesIO(data))

    tarball_hash = hashlib.sha256(tarball_path.read_bytes()).hexdigest()
    print(f"Created {tarball_path} ({tarball_path.stat().st_size} bytes, sha256: {tarball_hash})")
    return tarball_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Package Modulo docker-compose release bundle (FAR-2345)",
    )
    parser.add_argument(
        "--version",
        required=True,
        help="Release version in X.Y.Z form (e.g. 0.4.0)",
    )
    parser.add_argument(
        "--tag",
        default=None,
        help="Immutable container image tag to hard-pin (defaults to v<VERSION>)",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory to write the release tar.gz (default: current directory)",
    )
    parser.add_argument(
        "--compose-file",
        default=None,
        help=f"Path to source {_COMPOSE_FILENAME}",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to source .env.example",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        build_compose_release(
            version=args.version,
            tag=args.tag,
            output_dir=args.output_dir,
            compose_file=args.compose_file,
            env_file=args.env_file,
        )
        return 0
    except Exception as exc:
        print(f"Error packaging compose release: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
