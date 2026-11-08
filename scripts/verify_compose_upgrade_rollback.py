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
import re
import shlex
import subprocess  # nosec B404 - commands are fixed docker/curl argv lists
import sys
import time
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


#: Taint barrier for the operator-supplied compose project name (S8705).
#:
#: The harness forwards the project name into a ``docker compose argv``. Bound
#: it to its legitimate character set with ``re.fullmatch`` and pass only the
#: fresh match result to the child process, so a crafted value can never be
#: parsed as a CLI flag or carry an argument-injection payload
#: (pythonsecurity:S8705). Mirrors the established pattern in
#: ``scripts/fast_lane_classify.py``.
_PROJECT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _safe_project_name(value: str) -> str:
    """Return *value* only when it is a safe compose project name."""
    matched = _PROJECT_NAME_RE.fullmatch(value or "")
    if matched is None:
        raise ValidationError("--project-name must contain only letters, numbers, and hyphens")
    return matched.group(0)


def build_compose_command(compose_file: Path, project_name: str, *args: str, env_file: Path | None = None) -> list[str]:
    """Build an isolated compose command using the selected environment file."""
    command = ["docker", "compose", "-p", project_name]
    if env_file is not None:
        command.extend(["--env-file", str(env_file)])
    command.extend(["-f", str(compose_file), *args])
    return command


_INVALID_SECRET_KEY = "intentionally-invalid-secret"


def _redact(text: str, secrets: set[str]) -> str:
    """Redact configured secret values without emitting them to evidence."""

    redacted = text
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _parse_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip():
            values[name.strip()] = value.strip().strip('"').strip("'")
    return values


# Only credential-bearing variables are redacted from the evidence transcript.
# Operator flags such as TAG and PORT are documented provenance (the transcript
# must show which immutable images ran), so their values are never in this set.
_CREDENTIAL_NAME_PARTS = ("SECRET", "PASSWORD", "TOKEN", "FERNET", "CREDENTIAL", "PRIVATE_KEY", "API_KEY")


def _is_credential_name(name: str) -> bool:
    upper = name.strip().upper()
    return any(part in upper for part in _CREDENTIAL_NAME_PARTS) or upper.endswith("_KEY")


def _secret_values(env: dict[str, str] | None) -> set[str]:
    """Collect values of credential-bearing variables only.

    ``TAG``/``PORT``/``COMPOSE_FILE`` and other operator flags are deliberately
    excluded: redacting them would rewrite the transcript's own provenance.
    """
    if env is None:
        return set()
    return {value for name, value in env.items() if value and _is_credential_name(name)}


def _secret_values_from_file(path: Path) -> set[str]:
    """Redact every configured env-file value, including non-obvious credentials."""
    return {value for value in _parse_env_values(path).values() if value}


class Evidence:
    """Write bounded, machine-readable command evidence and human-readable logs."""

    def __init__(self, directory: Path, *, secrets: set[str] | None = None) -> None:
        self.directory = directory
        if self.directory.exists():
            if any(self.directory.iterdir()):
                raise ValidationError(f"evidence directory is not empty: {self.directory}")
        else:
            self.directory.mkdir(parents=True)
        self.transcript = self.directory / "commands.jsonl"
        self._counter = 0
        self._secrets = set(secrets or ())

    def add_secrets(self, secrets: set[str]) -> None:
        self._secrets.update(secrets)

    def record(self, result: CommandResult, *, outcome: str) -> None:
        self._counter += 1
        stem = f"{self._counter:02d}-{result.name}"
        stdout = _redact(result.stdout, self._secrets)
        stderr = _redact(result.stderr, self._secrets)
        argv = tuple(_redact(value, self._secrets) for value in result.argv)
        (self.directory / f"{stem}.stdout").write_text(stdout, encoding="utf-8")
        (self.directory / f"{stem}.stderr").write_text(stderr, encoding="utf-8")
        row = {
            "at": datetime.now(UTC).isoformat(),
            "name": result.name,
            "argv": list(argv),
            "command": shlex.join(argv),
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
            _redact(json.dumps(metadata, indent=2, sort_keys=True) + "\n", self._secrets), encoding="utf-8"
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
        self.evidence.add_secrets(_secret_values(env))
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
        """Poll readiness until the requested state or the bounded deadline.

        ``expected=True`` waits for a healthy HTTP 200. ``expected=False`` waits
        until the readiness endpoint is reachable but no longer reports 200 -
        the documented "readiness is no longer HTTP 200" contract. That covers
        the real failed-upgrade failure mode: an invalid ``SECRET_KEY`` crashes
        the backend before it binds its port, so the all-in-one proxy answers
        ``502`` for the whole crash-loop window rather than a bare ``503``, and
        any reachable non-200 status satisfies the assertion.
        """
        url = f"http://127.0.0.1:{self.port}/healthz/ready"
        argv = [
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            str(max(1, min(10, self.timeout))),
            "--output",
            os.devnull,
            "--write-out",
            "%{http_code}",
            url,
        ]
        deadline = time.monotonic() + self.timeout
        poll_interval = min(0.25, max(0.01, self.timeout / 10))
        attempt = 0
        last_code = "<none>"
        last_returncode = 0
        while True:
            attempt += 1
            raw = self.runner(
                argv,
                cwd=self.compose_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(1, min(10, self.timeout)),
                check=False,
            )
            result = CommandResult(
                name + f"-attempt-{attempt}", tuple(argv), raw.returncode, raw.stdout or "", raw.stderr or ""
            )
            self.evidence.record(
                result,
                outcome="success" if raw.returncode == 0 else f"unreachable:{raw.returncode}",
            )
            code = result.stdout.strip()
            last_code = code or "<none>"
            last_returncode = raw.returncode
            reachable = raw.returncode == 0
            is_ready = reachable and code == "200"
            reached_expected = is_ready if expected else (reachable and code.isdigit() and not is_ready)
            if reached_expected:
                self.evidence.record(
                    CommandResult(name + "-assertion", (url,), 0, f"HTTP {code}\n", ""),
                    outcome="ready" if is_ready else "not-ready",
                )
                return is_ready
            if time.monotonic() >= deadline:
                break
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
        wanted = "ready (HTTP 200)" if expected else "not ready (any reachable non-200)"
        raise ValidationError(
            f"{name}: expected {wanted} before deadline, received HTTP {last_code} (curl exit {last_returncode})"
        )

    def _assert_failed_upgrade_config(self, text: str, name: str) -> None:
        if self.new_tag not in text:
            raise ValidationError(f"{name} compose config did not use the requested new tag")
        if _INVALID_SECRET_KEY not in text:
            raise ValidationError(f"{name} compose config did not receive the intentionally invalid SECRET_KEY")

    def _assert_failed_upgrade_logs(self, result: CommandResult) -> None:
        combined = f"{result.stdout}\n{result.stderr}"
        expected = "SECRET_KEY must be at least"
        if expected not in combined:
            raise ValidationError("failed-upgrade logs did not contain the expected invalid SECRET_KEY startup failure")

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
        config = self.compose(
            "compose-config-invalid-secret",
            self.new_tag,
            "config",
            expected="success",
            secret_key=_INVALID_SECRET_KEY,
        )
        config_text = f"{config.stdout}\n{config.stderr}"
        self._assert_failed_upgrade_config(config_text, "invalid-secret")
        self.compose("clean-install-old", self.old_tag, "up", "-d", "--remove-orphans")
        self.ready("clean-install-ready", expected=True)

        self.compose("upgrade-new", self.new_tag, "up", "-d", "--remove-orphans")
        self.ready("upgrade-ready", expected=True)

        # The invalid key is deliberately too short for Settings. --force-recreate
        # ensures Compose replaces the healthy new container instead of treating
        # the command as a no-op. A successful command is not enough: readiness
        # must become unavailable before rollback is accepted. The backend
        # crash-loops before binding its port, so the proxy serves 502 for the
        # whole window - assert "no longer 200" rather than a single status code.
        self.compose(
            "failed-upgrade",
            self.new_tag,
            "up",
            "-d",
            "--force-recreate",
            "--no-deps",
            "modulo",
            expected="success",
            secret_key=_INVALID_SECRET_KEY,
        )
        self.ready("failed-upgrade-not-ready", expected=False)
        failed_logs = self.compose("failed-upgrade-logs", self.new_tag, "logs", "--no-color", "--tail=200", "modulo")
        self._assert_failed_upgrade_logs(failed_logs)

        self.compose("rollback-old", self.old_tag, "up", "-d", "--remove-orphans")
        self.ready("rollback-ready", expected=True)
        self.compose("rollback-logs", self.old_tag, "logs", "--no-color", "--tail=200", "modulo")


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
    project_name = _safe_project_name(args.project_name)

    evidence_dir = args.evidence_dir.resolve()
    evidence_secrets = _secret_values(dict(os.environ)) | _secret_values_from_file(env_file)
    # Never redact the operator flags the harness itself sets, even if an env
    # file happens to define the same value: they are documented provenance.
    evidence_secrets -= {old_tag, new_tag, args.port}
    evidence_secrets.discard("")
    evidence = Evidence(evidence_dir, secrets=evidence_secrets)
    matrix = Matrix(
        compose_file=compose_file,
        compose_dir=compose_dir,
        env_file=env_file,
        project_name=project_name,
        old_tag=old_tag,
        new_tag=new_tag,
        evidence=evidence,
        port=args.port,
        timeout=args.timeout,
    )
    cleanup_env = {**os.environ, "TAG": old_tag, "PORT": args.port}
    cleanup = build_compose_command(
        compose_file,
        project_name,
        "down",
        "--volumes",
        "--remove-orphans",
        env_file=env_file,
    )
    primary_error: BaseException | None = None
    try:
        matrix.run()
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            result = subprocess.run(
                cleanup,
                cwd=compose_dir,
                env=cleanup_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=args.timeout,
                check=False,
            )  # nosec B603 - fixed compose cleanup command
            evidence.record(
                CommandResult("cleanup", tuple(cleanup), result.returncode, result.stdout or "", result.stderr or ""),
                outcome="cleanup-success" if result.returncode == 0 else f"cleanup-failed:{result.returncode}",
            )
            if result.returncode != 0 and primary_error is None:
                raise ValidationError(f"cleanup failed with exit {result.returncode}")
        except subprocess.TimeoutExpired as exc:
            if primary_error is None:
                raise ValidationError("cleanup timed out; owned containers and volumes may remain") from exc
            print(f"cleanup timed out after primary failure: {exc}", file=sys.stderr)
        except OSError as exc:
            if primary_error is None:
                raise ValidationError(f"cleanup could not run: {exc}") from exc
            print(f"cleanup could not run after primary failure: {exc}", file=sys.stderr)

    result_file = evidence.directory / "RESULT"
    if primary_error is not None:
        return 1
    if result_file.exists():
        return 0
    result_file.write_text(
        "PASS clean-install -> upgrade -> intentionally failed upgrade -> rollback -> rollback verification\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValidationError, subprocess.CalledProcessError) as exc:
        print(f"upgrade/rollback validation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
