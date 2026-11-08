#!/usr/bin/env python3
"""Run the disposable Docker Compose upgrade/failure/rollback acceptance matrix.

The harness deliberately runs against a disposable compose project and records
host, image, command, outcome, and container-log evidence.  It proves the
operator-visible sequence rather than asserting that a compose command returned
zero: clean install (old tag), upgrade (new tag), an intentionally broken
upgrade (invalid SECRET_KEY), rollback (old tag), and rollback readiness.

Usage (from an extracted compose release directory):

    python verify_compose_upgrade_rollback.py \
      --compose-file docker-compose.prod.yml \
      --old-tag v0.3.1 --new-tag v0.3.2 \
      --evidence-dir evidence/compose-upgrade

The compose directory must contain the generated `.env` file.  The harness
always tears down its project, including volumes, in a finally block.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import subprocess  # nosec B404 - commands are fixed docker/curl argv lists
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ValidationError(RuntimeError):
    """Raised when the matrix cannot prove one of its required transitions."""


@dataclass(frozen=True)
class CommandResult:
    """Captured result for one command in the evidence transcript."""

    name: str
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def validate_tag(tag: str, *, name: str) -> str:
    """Require a non-empty immutable-looking release tag."""
    value = tag.strip()
    if not value:
        raise ValidationError(f"{name} must not be empty")
    if value.lower() in {"latest", "stable"}:
        raise ValidationError(f"{name} must be immutable, not {value!r}")
    return value


def build_compose_command(compose_file: Path, project_name: str, *args: str, env_file: Path | None = None) -> list[str]:
    """Build an isolated compose command using the selected environment file."""
    command = ["docker", "compose", "-p", project_name]
    if env_file is not None:
        command.extend(["--env-file", str(env_file)])
    command.extend(["-f", str(compose_file), *args])
    return command


class Evidence:
    """Write bounded, machine-readable command evidence and human-readable logs."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.transcript = self.directory / "commands.jsonl"
        self._counter = 0

    def record(self, result: CommandResult, *, outcome: str) -> None:
        self._counter += 1
        stem = f"{self._counter:02d}-{result.name}"
        (self.directory / f"{stem}.stdout").write_text(result.stdout, encoding="utf-8")
        (self.directory / f"{stem}.stderr").write_text(result.stderr, encoding="utf-8")
        row = {
            "at": datetime.now(UTC).isoformat(),
            "name": result.name,
            "argv": list(result.argv),
            "command": shlex.join(result.argv),
            "returncode": result.returncode,
            "outcome": outcome,
            "stdout": f"{stem}.stdout",
            "stderr": f"{stem}.stderr",
        }
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    def write_metadata(self, *, old_tag: str, new_tag: str, project_name: str, compose_file: Path) -> None:
        metadata = {
            "recorded_at": datetime.now(UTC).isoformat(),
            "host": platform.node(),
            "os": platform.platform(),
            "python": sys.version,
            "compose_file": str(compose_file.resolve()),
            "old_tag": old_tag,
            "new_tag": new_tag,
            "project_name": project_name,
            "cwd": str(Path.cwd()),
        }
        (self.directory / "metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


class Matrix:
    """Execute the compose acceptance sequence with explicit failure assertions."""

    def __init__(
        self,
        *,
        compose_file: Path,
        compose_dir: Path,
        env_file: Path,
        project_name: str,
        old_tag: str,
        new_tag: str,
        evidence: Evidence,
        port: str,
        timeout: int,
        runner: Any = subprocess.run,
    ) -> None:
        self.compose_file = compose_file
        self.compose_dir = compose_dir
        self.env_file = env_file
        self.project_name = project_name
        self.old_tag = old_tag
        self.new_tag = new_tag
        self.evidence = evidence
        self.port = port
        self.timeout = timeout
        self.runner = runner

    def command(
        self, name: str, argv: list[str], *, expected: str = "success", env: dict[str, str] | None = None
    ) -> CommandResult:
        result = self.runner(
            argv,
            cwd=self.compose_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
            check=False,
        )
        captured = CommandResult(name, tuple(argv), result.returncode, result.stdout or "", result.stderr or "")
        self.evidence.record(captured, outcome=expected if result.returncode == 0 else f"failed:{result.returncode}")
        if expected == "success" and result.returncode != 0:
            raise ValidationError(f"{name} failed with exit {result.returncode}; see evidence/{name}.stderr")
        if expected == "failure" and result.returncode == 0:
            raise ValidationError(f"{name} unexpectedly succeeded; the failed-upgrade proof is absent")
        return captured

    def compose(
        self, name: str, tag: str, *args: str, expected: str = "success", secret_key: str | None = None
    ) -> CommandResult:
        env = dict(os.environ)
        env["TAG"] = tag
        env["PORT"] = self.port
        env["COMPOSE_FILE"] = str(self.compose_file)
        if secret_key is not None:
            env["SECRET_KEY"] = secret_key
        argv = build_compose_command(self.compose_file, self.project_name, *args, env_file=self.env_file)
        return self.command(name, argv, expected=expected, env=env)

    def ready(self, name: str, *, expected: bool) -> bool:
        url = f"http://127.0.0.1:{self.port}/healthz/ready"
        argv = [
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            "10",
            "--output",
            os.devnull,
            "--write-out",
            "%{http_code}",
            url,
        ]
        raw = self.runner(
            argv,
            cwd=self.compose_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
            check=False,
        )
        result = CommandResult(name, tuple(argv), raw.returncode, raw.stdout or "", raw.stderr or "")
        self.evidence.record(result, outcome="success" if raw.returncode == 0 else f"unreachable:{raw.returncode}")
        code = result.stdout.strip()
        is_ready = raw.returncode == 0 and code == "200"
        if is_ready != expected:
            wanted = "ready (HTTP 200)" if expected else "not ready"
            raise ValidationError(f"{name}: expected {wanted}, received HTTP {code or '<none>'}")
        self.evidence.record(
            CommandResult(name + "-assertion", (url,), 0, f"HTTP {code}\n", ""),
            outcome="ready" if is_ready else "not-ready",
        )
        return is_ready

    def run(self) -> None:
        self.evidence.write_metadata(
            old_tag=self.old_tag,
            new_tag=self.new_tag,
            project_name=self.project_name,
            compose_file=self.compose_file,
        )
        self.command("docker-version", ["docker", "version"], expected="success")
        self.command(
            "compose-config-old",
            build_compose_command(self.compose_file, self.project_name, "config", env_file=self.env_file),
            expected="success",
            env={**os.environ, "TAG": self.old_tag, "PORT": self.port},
        )
        self.compose("clean-install-old", self.old_tag, "up", "-d", "--remove-orphans")
        self.ready("clean-install-ready", expected=True)

        self.compose("upgrade-new", self.new_tag, "up", "-d", "--remove-orphans")
        self.ready("upgrade-ready", expected=True)

        # The invalid key is deliberately too short for Settings. --force-recreate
        # ensures Compose replaces the healthy new container instead of treating
        # the command as a no-op. A successful command is not enough: readiness
        # must become unavailable before rollback is accepted.
        self.compose(
            "failed-upgrade",
            self.new_tag,
            "up",
            "-d",
            "--force-recreate",
            "--no-deps",
            "modulo",
            expected="success",
            secret_key="intentionally-invalid-secret",
        )
        self.ready("failed-upgrade-not-ready", expected=False)
        self.compose("failed-upgrade-logs", self.new_tag, "logs", "--no-color", "--tail=200", "modulo")

        self.compose("rollback-old", self.old_tag, "up", "-d", "--remove-orphans")
        self.ready("rollback-ready", expected=True)
        self.compose("rollback-logs", self.old_tag, "logs", "--no-color", "--tail=200", "modulo")
        (self.evidence.directory / "RESULT").write_text(
            "PASS clean-install -> upgrade -> intentionally failed upgrade -> rollback -> rollback verification\n",
            encoding="utf-8",
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--old-tag", required=True)
    parser.add_argument("--new-tag", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--project-name", default=f"modulo-upgrade-{os.getpid()}")
    parser.add_argument("--port", default="18080")
    parser.add_argument("--timeout", type=int, default=120)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    compose_file = args.compose_file.resolve()
    if not compose_file.is_file():
        raise ValidationError(f"compose file does not exist: {compose_file}")
    compose_dir = compose_file.parent
    env_file = (args.env_file or compose_dir / ".env").resolve()
    if not env_file.is_file():
        raise ValidationError(f"compose environment file does not exist: {env_file}")
    old_tag = validate_tag(args.old_tag, name="--old-tag")
    new_tag = validate_tag(args.new_tag, name="--new-tag")
    if old_tag == new_tag:
        raise ValidationError("--old-tag and --new-tag must be different")
    if not args.project_name.replace("-", "").isalnum():
        raise ValidationError("--project-name must contain only letters, numbers, and hyphens")

    evidence = Evidence(args.evidence_dir.resolve())
    matrix = Matrix(
        compose_file=compose_file,
        compose_dir=compose_dir,
        env_file=env_file,
        project_name=args.project_name,
        old_tag=old_tag,
        new_tag=new_tag,
        evidence=evidence,
        port=args.port,
        timeout=args.timeout,
    )
    cleanup_env = {**os.environ, "TAG": old_tag, "PORT": args.port}
    cleanup = build_compose_command(
        compose_file,
        args.project_name,
        "down",
        "--volumes",
        "--remove-orphans",
        env_file=env_file,
    )
    try:
        matrix.run()
    finally:
        result = subprocess.run(cleanup, cwd=compose_dir, env=cleanup_env, capture_output=True, text=True, check=False)  # nosec B603 - fixed compose cleanup command
        evidence.record(
            CommandResult("cleanup", tuple(cleanup), result.returncode, result.stdout or "", result.stderr or ""),
            outcome="cleanup",
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValidationError, subprocess.CalledProcessError) as exc:
        print(f"upgrade/rollback validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
