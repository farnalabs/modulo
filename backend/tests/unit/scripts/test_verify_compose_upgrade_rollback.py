"""Contract tests for the Docker Compose upgrade/rollback acceptance harness."""

from __future__ import annotations

import json
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "verify_compose_upgrade_rollback.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/verify_compose_upgrade_rollback.py)")

loader = SourceFileLoader("verify_compose_upgrade_rollback", str(script_path))
spec = spec_from_file_location("verify_compose_upgrade_rollback", script_path, loader=loader)
if spec is None:
    raise RuntimeError("Could not load verify_compose_upgrade_rollback.py")
module = module_from_spec(spec)
sys.modules["verify_compose_upgrade_rollback"] = module
loader.exec_module(module)

Evidence = module.Evidence
Matrix = module.Matrix
ValidationError = module.ValidationError
build_compose_command = module.build_compose_command
validate_tag = module.validate_tag


def test_validate_tag_rejects_moving_tags() -> None:
    with pytest.raises(ValidationError, match="immutable"):
        validate_tag("latest", name="tag")
    with pytest.raises(ValidationError, match="empty"):
        validate_tag("  ", name="tag")


def test_build_compose_command_is_project_isolated(tmp_path: Path) -> None:
    command = build_compose_command(tmp_path / "docker-compose.prod.yml", "proof-123", "up", "-d")
    assert command[:6] == [
        "docker",
        "compose",
        "-p",
        "proof-123",
        "-f",
        str(tmp_path / "docker-compose.prod.yml"),
    ]
    assert command[-2:] == ["up", "-d"]


def test_matrix_records_every_required_transition(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(tuple(argv))
        if argv[0] == "curl":
            phase = next(item for item in reversed(calls) if item[0] == "docker" and "up" in item)
            status = "503" if "--force-recreate" in phase else "200"
            return SimpleNamespace(returncode=0, stdout=status, stderr="")
        if argv[0] == "docker" and "logs" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout="ValidationError: SECRET_KEY must be at least 32 bytes",
                stderr="",
            )
        if argv[0] == "docker" and "config" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout="image: ghcr.io/farnalabs/modulo:v1.1.0\nSECRET_KEY: intentionally-invalid-secret",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    evidence = Evidence(tmp_path / "evidence")
    matrix = Matrix(
        compose_file=tmp_path / "docker-compose.prod.yml",
        compose_dir=tmp_path,
        env_file=tmp_path / ".env",
        project_name="proof-123",
        old_tag="v1.0.0",
        new_tag="v1.1.0",
        evidence=evidence,
        port="18080",
        timeout=10,
        runner=runner,
    )
    matrix.run()

    names = [json.loads(line)["name"] for line in evidence.transcript.read_text(encoding="utf-8").splitlines()]
    assert names == [
        "docker-version",
        "compose-config-old",
        "compose-config-invalid-secret",
        "clean-install-old",
        "clean-install-ready",
        "clean-install-ready-assertion",
        "upgrade-new",
        "upgrade-ready",
        "upgrade-ready-assertion",
        "failed-upgrade",
        "failed-upgrade-not-ready",
        "failed-upgrade-not-ready-assertion",
        "failed-upgrade-logs",
        "rollback-old",
        "rollback-ready",
        "rollback-ready-assertion",
        "rollback-logs",
    ]
    assert any("--force-recreate" in call for call in calls)
    (evidence.directory / "RESULT").write_text(
        "PASS clean-install -> upgrade -> intentionally failed upgrade -> rollback -> rollback verification\n",
        encoding="utf-8",
    )
    assert "PASS clean-install -> upgrade -> intentionally failed upgrade -> rollback -> rollback verification" in (
        evidence.directory / "RESULT"
    ).read_text(encoding="utf-8")


def test_evidence_redacts_secret_literals(tmp_path: Path) -> None:
    evidence = Evidence(tmp_path / "evidence", secrets={"db-password", "jwt-secret"})
    evidence.record(
        module.CommandResult(
            "compose-config",
            ("docker", "compose", "config", "db-password"),
            0,
            "SECRET_KEY=db-password\n",
            "FERNET_KEY=jwt-secret\n",
        ),
        outcome="success",
    )
    rendered = "\n".join(path.read_text(encoding="utf-8") for path in evidence.directory.iterdir())
    assert "db-password" not in rendered
    assert "jwt-secret" not in rendered
    assert "[REDACTED]" in rendered


def test_evidence_rejects_stale_directory(tmp_path: Path) -> None:
    directory = tmp_path / "evidence"
    directory.mkdir()
    (directory / "RESULT").write_text("old run\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="not empty"):
        Evidence(directory)


def test_matrix_rejects_a_healthy_failed_upgrade(tmp_path: Path) -> None:
    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        if argv[0] == "curl":
            return SimpleNamespace(returncode=0, stdout="200", stderr="")
        if argv[0] == "docker" and "config" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout="image: ghcr.io/farnalabs/modulo:v1.1.0",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    evidence = Evidence(tmp_path / "evidence")
    matrix = Matrix(
        compose_file=tmp_path / "docker-compose.prod.yml",
        compose_dir=tmp_path,
        env_file=tmp_path / ".env",
        project_name="proof-123",
        old_tag="v1.0.0",
        new_tag="v1.1.0",
        evidence=evidence,
        port="18080",
        timeout=10,
        runner=runner,
    )
    with pytest.raises(ValidationError, match="expected HTTP 503"):
        matrix.run()


def test_matrix_records_failed_compose_commands_as_failures(tmp_path: Path) -> None:
    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        if argv[0] == "docker" and "version" in argv:
            return SimpleNamespace(returncode=1, stdout="", stderr="daemon unavailable")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    evidence = Evidence(tmp_path / "evidence")
    matrix = Matrix(
        compose_file=tmp_path / "docker-compose.prod.yml",
        compose_dir=tmp_path,
        env_file=tmp_path / ".env",
        project_name="proof-123",
        old_tag="v1.0.0",
        new_tag="v1.1.0",
        evidence=evidence,
        port="18080",
        timeout=10,
        runner=runner,
    )
    with pytest.raises(ValidationError, match="docker-version failed"):
        matrix.run()
    rows = [json.loads(line) for line in evidence.transcript.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["returncode"] == 1
    assert rows[0]["outcome"] == "failed:1"
