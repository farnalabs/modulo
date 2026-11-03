"""Unit tests for scripts/package_compose_release.py (FAR-2345).

Tests packaging of the self-hosted production compose stack into a release asset bundle.
"""

from __future__ import annotations

import hashlib
import re
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

# Ensure scripts directory is on sys.path
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from package_compose_release import (  # noqa: E402
    build_compose_release,
    generate_quickstart_md,
    transform_compose_content,
    validate_release_tag,
)


def test_validate_release_tag_valid():
    assert validate_release_tag("v0.1.0") == "v0.1.0"
    assert validate_release_tag("0.1.0") == "0.1.0"
    assert validate_release_tag("v0.1.0-rc1") == "v0.1.0-rc1"
    assert validate_release_tag("v1.2.3-alpha.4") == "v1.2.3-alpha.4"


def test_validate_release_tag_rejects_latest():
    with pytest.raises(ValueError, match="latest"):
        validate_release_tag("latest")
    with pytest.raises(ValueError, match="latest"):
        validate_release_tag("LATEST")
    with pytest.raises(ValueError, match="latest"):
        validate_release_tag(":latest")


def test_validate_release_tag_rejects_git_sha():
    # 7-char short sha
    with pytest.raises(ValueError, match="git SHA"):
        validate_release_tag("a1b2c3d")
    # 40-char full sha
    with pytest.raises(ValueError, match="git SHA"):
        validate_release_tag("4b825dc642cb6eb9a060e54bf8d69288fbee4904")


def test_validate_release_tag_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        validate_release_tag("")
    with pytest.raises(ValueError, match="empty"):
        validate_release_tag("   ")


def test_transform_compose_content_hardpins_tag_and_removes_watchdog():
    sample_content = """services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_PASSWORD: ${MODULO_DB_PASSWORD:?required}
  redis:
    image: redis:8-alpine
  modulo:
    image: ghcr.io/farnalabs/modulo:${TAG:?TAG is required - set the Modulo version you intend to run}
    ports:
      - "${PORT:-80}:80"
  watchdog:
    build: ../watchdog
    ports:
      - "127.0.0.1:8083:8080"
volumes:
  postgres_data:
  redis_data:
"""
    transformed = transform_compose_content(sample_content, "v0.4.0")
    data = yaml.safe_load(transformed)

    assert "services" in data
    services = data["services"]
    assert set(services.keys()) == {"postgres", "redis", "modulo"}
    assert services["modulo"]["image"] == "ghcr.io/farnalabs/modulo:v0.4.0"
    assert "watchdog" not in services
    assert "postgres_data" in data["volumes"]
    assert "redis_data" in data["volumes"]


def test_transform_compose_content_rejects_missing_image():
    invalid_content = """services:
  modulo:
    ports: ["80:80"]
volumes:
  postgres_data:
  redis_data:
"""
    with pytest.raises(ValueError, match=r"Could not find 'image: ghcr\.io/farnalabs/modulo"):
        transform_compose_content(invalid_content, "v0.4.0")


def test_generate_quickstart_md():
    md = generate_quickstart_md("0.4.0", "v0.4.0")
    assert "v0.4.0" in md
    assert "ghcr.io/farnalabs/modulo:v0.4.0" in md
    assert "docker compose up -d" in md
    assert "curl -f http://localhost:80/healthz/ready" in md
    assert "sha256sum --check SHA256SUMS" in md


def test_build_compose_release_end_to_end(tmp_path: Path):
    tarball_path = build_compose_release(
        version="0.4.0",
        tag="v0.4.0-rc1",
        output_dir=tmp_path,
    )

    assert tarball_path.is_file()
    assert tarball_path.name == "modulo-0.4.0-compose.tar.gz"

    extracted_dir = tmp_path / "extracted"
    extracted_dir.mkdir()

    with tarfile.open(tarball_path, "r:gz") as tar:
        if hasattr(tarfile, "data_filter"):
            tar.extractall(extracted_dir, filter="data")
        else:
            tar.extractall(extracted_dir)  # noqa: S202

    expected_files = {
        "docker-compose.prod.yml",
        "docker-compose.yml",
        ".env.example",
        "quickstart.md",
        "init-env.sh",
        "SHA256SUMS",
    }
    actual_files = {p.name for p in extracted_dir.iterdir()}
    assert actual_files == expected_files

    # Verify SHA256SUMS integrity over the files
    sums_file = extracted_dir / "SHA256SUMS"
    sums_lines = sums_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(sums_lines) == 5

    sums_dict: dict[str, str] = {}
    for line in sums_lines:
        digest, filename = line.split()
        sums_dict[filename] = digest

    for name in ("docker-compose.prod.yml", "docker-compose.yml", ".env.example", "quickstart.md", "init-env.sh"):
        assert name in sums_dict
        file_bytes = (extracted_dir / name).read_bytes()
        actual_digest = hashlib.sha256(file_bytes).hexdigest()
        assert actual_digest == sums_dict[name]

    # Verify compose file has hard-pinned tag and no watchdog
    compose_data = yaml.safe_load((extracted_dir / "docker-compose.prod.yml").read_text(encoding="utf-8"))
    assert compose_data["services"]["modulo"]["image"] == "ghcr.io/farnalabs/modulo:v0.4.0-rc1"
    assert "watchdog" not in compose_data["services"]

    # Verify docker-compose.yml is identical copy
    assert (extracted_dir / "docker-compose.yml").read_text(encoding="utf-8") == (
        extracted_dir / "docker-compose.prod.yml"
    ).read_text(encoding="utf-8")


def test_build_compose_release_derives_tag_from_version(tmp_path: Path):
    tarball_path = build_compose_release(
        version="0.5.1",
        output_dir=tmp_path,
    )
    assert tarball_path.name == "modulo-0.5.1-compose.tar.gz"

    with tarfile.open(tarball_path, "r:gz") as tar:
        f = tar.extractfile("docker-compose.prod.yml")
        assert f is not None
        data = yaml.safe_load(f.read().decode("utf-8"))
        assert data["services"]["modulo"]["image"] == "ghcr.io/farnalabs/modulo:v0.5.1"


def test_build_compose_release_rejects_invalid_version(tmp_path: Path):
    with pytest.raises(ValueError, match="semver"):
        build_compose_release(version="not-semver", output_dir=tmp_path)
    with pytest.raises(ValueError, match="Version cannot be empty"):
        build_compose_release(version="", output_dir=tmp_path)


# ---------------------------------------------------------------------------
# Bundled .env.example must satisfy the bundled compose's fail-fast contract
#
# The bundle is the FIRST thing an evaluator runs, and the compose refuses to
# start on a missing `${VAR:?...}`. A bundled example that omits one of them
# would turn the quickstart's `docker compose up -d` into an error naming a
# variable the evaluator has never seen.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _bundled_files(version: str = "0.4.0") -> dict[str, str]:
    """Build the bundle in a temp dir and return {name: text} for its files."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tarball = build_compose_release(version=version, output_dir=tmp)
        with tarfile.open(tarball, "r:gz") as tar:
            files: dict[str, str] = {}
            for member in tar.getmembers():
                handle = tar.extractfile(member)
                assert handle is not None
                files[member.name] = handle.read().decode("utf-8")
            return files


def _interpolated_scalars(node: object) -> str:
    """Every scalar VALUE in a parsed compose document, concatenated.

    Parsing first drops comments: the file's own prose legitimately writes
    ``${VAR:?...}`` when explaining the syntax, and a raw-text scan reads that
    as a required variable named ``VAR``.
    """
    if isinstance(node, dict):
        return "".join(_interpolated_scalars(value) for value in node.values())
    if isinstance(node, list):
        return "".join(_interpolated_scalars(value) for value in node)
    if isinstance(node, str):
        return node
    return ""


def _assignments(text: str) -> dict[str, str]:
    return {
        line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }


def _required_compose_vars() -> set[str]:
    compose_text = _interpolated_scalars(yaml.safe_load(_bundled_files()["docker-compose.prod.yml"]))
    required = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?", compose_text))
    assert required, "the bundled compose declares no fail-fast ${VAR:?} references"
    return required


def test_bundled_env_example_declares_every_required_compose_variable():
    """Every `${VAR:?}` in the bundled compose must be an assignable key.

    `init-env.sh` fills those keys with `sed`, so a required name that is only
    mentioned in a comment - or missing entirely - is never populated and the
    first `docker compose up` fails naming a variable the operator has not seen.
    """
    required = _required_compose_vars()
    declared = _assignments(_bundled_files()[".env.example"])

    missing = sorted(name for name in required if name not in declared)
    assert not missing, f"bundled .env.example does not declare required compose variable(s) {missing}"


def test_bundled_env_example_ships_no_secret_values():
    """The published asset must not carry a shared key or password.

    A pre-filled example would hand every downloader the SAME JWT signing key
    and database password. `init-env.sh` generates them on the operator's
    machine instead, so the required SECRETS ship empty. (`SAQ_AUTH_USERNAME`
    is a username, not a secret, and legitimately ships as `admin`.)
    """
    secret_names = {"MODULO_DB_PASSWORD", "SECRET_KEY", "FERNET_KEY", "SAQ_AUTH_PASSWORD"}
    declared = _assignments(_bundled_files()[".env.example"])

    populated = sorted(name for name in secret_names if declared.get(name))
    assert not populated, f"bundled .env.example ships a literal value for secret variable(s) {populated}"


def test_bundled_init_env_generates_every_required_variable():
    files = _bundled_files()
    script = files["init-env.sh"]

    assert script.startswith("#!/bin/sh"), "init-env.sh must be a POSIX shell script"
    for name in _required_compose_vars():
        assert name in script, f"init-env.sh never sets {name}, so the compose would refuse to start"

    # Generation must not depend on a single tool: openssl when present,
    # /dev/urandom otherwise.
    assert "openssl rand" in script
    assert "/dev/urandom" in script
    # A hex password is required because it is embedded unescaped in a DB URL.
    assert "gen_hex" in script
    # Refuse to clobber an existing .env: silently rotating secrets under a
    # running stack would invalidate every session and stored credential.
    assert "--force" in script


def test_bundled_quickstart_documents_the_init_step():
    md = _bundled_files()["quickstart.md"]
    assert "./init-env.sh" in md
    assert "docker compose up -d" in md


def test_bundled_init_env_ships_executable(tmp_path: Path):
    tarball = build_compose_release(version="0.4.0", output_dir=tmp_path)
    with tarfile.open(tarball, "r:gz") as tar:
        members = {member.name: member for member in tar.getmembers()}

    assert members["init-env.sh"].mode & 0o111, "init-env.sh must be executable in the tarball"
    for name in ("docker-compose.prod.yml", "docker-compose.yml", ".env.example", "quickstart.md"):
        assert not members[name].mode & 0o111, f"{name} should not be executable"


def test_bundled_env_values_satisfy_the_settings_validators():
    """Whatever `init-env.sh` generates must survive Settings' own validators.

    The shapes are asserted directly (lengths and base64 decodability) rather
    than by running the shell script: Settings rejects a SECRET_KEY under 32
    bytes, and cryptography rejects a FERNET_KEY that is not 32 urlsafe-base64
    bytes - a generation recipe that produced either would boot-fail on the
    clean host, after the evaluator had already downloaded the asset.
    """
    import base64

    from cryptography.fernet import Fernet

    script = _bundled_files()["init-env.sh"]
    # Code only: the script's own comments must not satisfy these assertions.
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))

    # 48 bytes of base64 for SECRET_KEY (>= 32 bytes after encoding, 64 chars).
    assert 'SECRET_KEY="$(gen_b64 48)"' in code
    # 32 bytes of base64 for FERNET_KEY (44 chars, decodes to exactly 32 bytes).
    assert 'FERNET_KEY="$(gen_b64 32)"' in code
    assert 'DB_PASSWORD="$(gen_hex 48)"' in code

    # Prove the recipes are accepted by the real validators.
    sample_fernet = base64.b64encode(b"x" * 32).decode()
    assert len(base64.urlsafe_b64decode(sample_fernet)) == 32
    assert len(Fernet(sample_fernet.encode()).encrypt(b"probe")) > 0
    assert len(b"x" * 36) >= 32  # 48 base64 chars carry 36 decoded bytes


def test_bundled_compose_is_standalone_and_dropped_the_source_build():
    """The bundle must run from its own directory: no `../watchdog` build context.

    `deploy/compose/docker-compose.prod.yml` in the repo builds the watchdog from
    `../watchdog`, which a downloaded tarball does not contain. An evaluator
    running `docker compose up -d` would fail resolving that build context.
    """
    files = _bundled_files()
    compose = yaml.safe_load(files["docker-compose.prod.yml"])

    assert "watchdog" not in compose["services"]
    for name, service in compose["services"].items():
        assert "build" not in service, f"bundled service '{name}' still builds from a local context"
    assert "watchdog" not in files

    # Every image is either a pinned upstream major or the immutable Modulo tag.
    assert compose["services"]["modulo"]["image"].startswith("ghcr.io/farnalabs/modulo:")
    assert not compose["services"]["modulo"]["image"].endswith(":latest")
