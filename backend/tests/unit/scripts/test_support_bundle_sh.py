"""FAR-2306 — ``scripts/support-bundle.sh`` is EXECUTED, not string-matched.

The Compose support-bundle collector is the only diagnostic path a Compose user
has (no log volume is mounted for the backend or the workers, so logs vanish on
recreate). These tests run the shipped script for real, with a stubbed ``docker``
first on PATH, and inspect the zip it produces.

What is pinned here:

* the stack-down contract — exit 0, a bundle is written, ``MANIFEST.txt`` names
  the missing app source AND the reason (never a silent omission);
* repeat-collapsing and bounding of container logs;
* credential posture — ``.env`` / ``secrets.json`` are never copied, and a value
  that lives only in them never reaches a member;
* the app-merge contract — members from
  ``python -m farna.support.bundle --out ... --json`` are merged into the SAME
  zip, the module's ref line and ``bundle_format_version`` win, and its own
  MANIFEST/report are preserved under ``app/`` rather than dropped;
* a failed run leaves no archive behind that could be mistaken for a bundle.

The stub is a bash script that answers the exact ``docker`` / ``docker compose``
invocations the collector makes and logs every call, so an unexpected invocation
fails loudly instead of silently returning nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "support-bundle.sh"
#: Resolve bash once: the dependency tests deliberately strip PATH.
BASH = shutil.which("bash") or "/bin/bash"

pytestmark = pytest.mark.posix_only

DOCKER_STUB = r"""#!/usr/bin/env bash
# Stub `docker` for test_support_bundle_sh.py. Answers the exact invocations the
# collector makes; anything else exits non-zero so a new call is visible.
set -u
printf '%s\n' "$*" >>"${STUB_LOG:?}"

case "${1:-}" in
  version)
    if [[ "${2:-}" == "--format" ]]; then
      case "${3:-}" in
        *Server*) printf '%s\n' "${STUB_SERVER_VERSION:-27.0.0}" ;;
        *) printf '%s\n' "${STUB_CLIENT_VERSION:-27.0.0}" ;;
      esac
    else
      printf 'Client:\n Version: %s\nServer:\n Version: %s\n' \
        "${STUB_CLIENT_VERSION:-27.0.0}" "${STUB_SERVER_VERSION:-27.0.0}"
    fi
    ;;
  info)
    printf 'OperatingSystem=StubOS\nOSType=linux\nArchitecture=x86_64\nServerVersion=%s\n' \
      "${STUB_SERVER_VERSION:-27.0.0}"
    ;;
  inspect)
    case "${3:-}" in
      *State.Running*) printf '%s\n' "${STUB_RUNNING:-true}" ;;
      *com.docker.compose.project*) printf '%s\n' "${STUB_PROJECT:-stub-project}" ;;
      *) printf '%b\n' "${STUB_CONTAINER_LINE:-/stub-backend\tbackend\trunning\thealthy\t0\t0\tstub}" ;;
    esac
    ;;
  image)
    printf '    RepoTags=[stub:latest] RepoDigests=[stub@sha256:deadbeef] Id=sha256:abc\n'
    ;;
  volume)
    printf '  - stub_pgdata\n'
    ;;
  cp)
    cp "${STUB_APP_ZIP:?}" "${3:?}"
    ;;
  compose)
    shift
    while [[ $# -gt 0 ]]; do
      case "$1" in
        -f|-p) shift 2 ;;
        *) break ;;
      esac
    done
    sub="${1:-}"
    shift || true
    case "$sub" in
      version) printf 'Docker Compose version v9.9.9\n' ;;
      config)
        case "${1:-}" in
          --services)
            if [[ "${STUB_CONFIG_FAIL:-0}" == "1" ]]; then
              printf 'config failed: required variable missing\n' >&2
              exit 1
            fi
            printf '%s\n' "${STUB_SERVICES:-db backend saq-runner}" | tr ' ' '\n'
            ;;
          --volumes) printf '%s\n' "${STUB_VOLUMES:-pgdata}" ;;
          *) printf 'unexpected config invocation: %s\n' "$*" >&2; exit 1 ;;
        esac
        ;;
      ps)
        case " $* " in
          *" --format "*)
            printf '%s\n' "${STUB_IMAGES:-stub:latest}"
            ;;
          *" -q "*)
            if [[ " $* " == *" backend "* ]]; then
              printf '%s\n' "${STUB_BACKEND_ID:-}"
            else
              printf '%s\n' "${STUB_CONTAINER_IDS:-}"
            fi
            ;;
          *)
            printf 'NAME IMAGE COMMAND SERVICE CREATED STATUS PORTS\n'
            if [[ -n "${STUB_PS_ROW:-}" ]]; then printf '%s\n' "${STUB_PS_ROW}"; fi
            ;;
        esac
        ;;
      logs)
        if [[ -n "${STUB_LOGS_FILE:-}" ]]; then cat "${STUB_LOGS_FILE}"; fi
        if [[ "${STUB_LOGS_FAIL:-0}" == "1" ]]; then
          printf 'logs failed\n' >&2
          exit 1
        fi
        ;;
      images) printf 'CONTAINER REPOSITORY TAG IMAGE ID SIZE\n' ;;
      run)
        # One-off redaction filter (the contract with FAR-2305):
        #   docker compose run --rm -T --no-deps --entrypoint python backend \
        #     -m farna.support.bundle --redact-stdin
        # STUB_REDACT_STDIN_OK=1 makes this act as the module's stdin gate; the
        # default is UNAVAILABLE, which is what the collector must tolerate.
        if [[ "${STUB_REDACT_STDIN_OK:-0}" != "1" ]]; then
          printf 'Error response from daemon: pull access denied for modulo\n' >&2
          exit 1
        fi
        if [[ "${STUB_REDACT_STDIN_REFUSE:-0}" == "1" ]]; then
          printf 'refused: redaction gate refused member: unclassified high-entropy token\n' >&2
          exit 3
        fi
        sed -e 's/ghp_[A-Za-z0-9]*/[REDACTED:github-token]/g'
        ;;
      exec)
        case " $* " in
          *" date "*) printf '%s\n' "${STUB_CONTAINER_EPOCH:-1700000000}"; exit 0 ;;
        esac
        if [[ "${STUB_APP_OK:-0}" == "1" ]]; then
          printf '%s\n' "${STUB_APP_JSON:-}"
          exit 0
        fi
        printf 'No module named farna.support.bundle\n' >&2
        exit 1
        ;;
      *) printf 'unexpected compose subcommand: %s\n' "$sub" >&2; exit 1 ;;
    esac
    ;;
  *) printf 'unexpected docker invocation: %s\n' "$*" >&2; exit 1 ;;
esac
"""

COMPOSE_FIXTURE = """\
services:
  db:
    image: postgres:16
  backend:
    image: modulo:latest
  saq-runner:
    image: modulo:latest
volumes:
  pgdata:
"""


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _stub_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    """A PATH whose first entry holds the docker stub, plus the real PATH so the
    collector's coreutils (awk/sed/zip/unzip/date/...) resolve normally."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_executable(bin_dir / "docker", DOCKER_STUB)
    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "STUB_LOG": str(tmp_path / "docker.log"),
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    env.update(overrides)
    return env


def _compose_file(tmp_path: Path) -> Path:
    path = tmp_path / "docker-compose.yml"
    path.write_text(COMPOSE_FIXTURE, encoding="utf-8")
    return path


def _run(
    tmp_path: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, repo script under test
        [BASH, str(SCRIPT), *args],
        env=env if env is not None else _stub_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def _collect(tmp_path: Path, **overrides: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    out = tmp_path / "bundle.zip"
    env = _stub_env(tmp_path, **overrides)
    proc = _run(
        tmp_path,
        "--out",
        str(out),
        "--compose-file",
        str(_compose_file(tmp_path)),
        env=env,
    )
    return proc, out


def _members(archive: Path) -> list[str]:
    with zipfile.ZipFile(archive) as zf:
        return zf.namelist()


def _member_text(archive: Path, name: str) -> str:
    with zipfile.ZipFile(archive) as zf:
        return zf.read(name).decode("utf-8")


class TestUsageAndDependencies:
    def test_help_exits_zero_without_any_tools_on_path(self, tmp_path: Path) -> None:
        # --help must work on a machine where nothing else is installed.
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        env = {"PATH": str(empty), "HOME": str(tmp_path), "TMPDIR": str(tmp_path)}
        proc = _run(tmp_path, "--help", env=env)
        assert proc.returncode == 0
        assert "USAGE" in proc.stdout
        assert "--compose-file" in proc.stdout

    def test_unknown_argument_is_usage_error(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        env = {"PATH": str(empty), "HOME": str(tmp_path), "TMPDIR": str(tmp_path)}
        proc = _run(tmp_path, "--nonsense", env=env)
        assert proc.returncode == 2
        assert "unknown argument" in proc.stderr

    def test_missing_docker_is_named(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        env = {"PATH": str(empty), "HOME": str(tmp_path), "TMPDIR": str(tmp_path)}
        proc = _run(tmp_path, env=env)
        assert proc.returncode != 0
        assert "docker is required" in proc.stderr

    def test_missing_zip_is_named(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_executable(bin_dir / "docker", DOCKER_STUB)
        env = {
            "PATH": str(bin_dir),
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "STUB_LOG": str(tmp_path / "d.log"),
        }
        proc = _run(tmp_path, env=env)
        assert proc.returncode != 0
        assert "zip is required" in proc.stderr

    def test_failed_run_leaves_no_archive_behind(self, tmp_path: Path) -> None:
        # A missing dependency must not leave a file that looks like a bundle.
        out = tmp_path / "bundle.zip"
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        env = {"PATH": str(empty), "HOME": str(tmp_path), "TMPDIR": str(tmp_path)}
        proc = _run(tmp_path, "--out", str(out), env=env)
        assert proc.returncode != 0
        assert not out.exists()
        assert not list(tmp_path.glob("*.partial*"))


class TestStackDown:
    def test_exit_zero_and_bundle_written(self, tmp_path: Path) -> None:
        proc, out = _collect(tmp_path, STUB_CONTAINER_IDS="", STUB_BACKEND_ID="")
        assert proc.returncode == 0, proc.stderr
        assert out.is_file()
        assert zipfile.is_zipfile(out)

    def test_manifest_names_the_missing_app_source_and_reason(self, tmp_path: Path) -> None:
        proc, out = _collect(tmp_path, STUB_CONTAINER_IDS="", STUB_BACKEND_ID="")
        assert proc.returncode == 0, proc.stderr
        manifest = _member_text(out, "MANIFEST.txt")
        assert 'missing: ["app"]' in manifest
        assert "no 'backend' container exists" in manifest
        assert "bundle_format_version: 1" in manifest

    def test_report_json_is_machine_readable(self, tmp_path: Path) -> None:
        proc, out = _collect(tmp_path, STUB_CONTAINER_IDS="", STUB_BACKEND_ID="")
        assert proc.returncode == 0, proc.stderr
        report = json.loads(_member_text(out, "report.json"))
        assert report["bundle_format_version"] == 1
        assert report["app_bundle"]["collected"] is False
        assert [entry["source"] for entry in report["missing"]] == ["app"]

    def test_docker_members_are_present(self, tmp_path: Path) -> None:
        proc, out = _collect(tmp_path, STUB_CONTAINER_IDS="", STUB_BACKEND_ID="")
        assert proc.returncode == 0, proc.stderr
        names = _members(out)
        for expected in (
            "docker/compose-ps.txt",
            "docker/containers.txt",
            "docker/images.txt",
            "docker/volumes.txt",
            "docker/compose-context.txt",
            "host/facts.txt",
            "host/clock.txt",
        ):
            assert expected in names

    def test_stdout_is_ref_then_bundle_then_advice(self, tmp_path: Path) -> None:
        # A working gate keeps the ordinary three-line stdout contract.
        proc, out = _collect(
            tmp_path,
            STUB_CONTAINER_IDS="",
            STUB_BACKEND_ID="",
            STUB_REDACT_STDIN_OK="1",
        )
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.strip().splitlines()
        assert len(lines) == 3
        assert lines[0].startswith("modulo support bundle 1 ref=MSB-1-")
        assert lines[1].startswith(f"bundle: {out} (")
        assert "before sending" in lines[2]

    def test_stdout_names_the_withheld_logs_when_the_gate_is_unavailable(self, tmp_path: Path) -> None:
        proc, out = _collect(tmp_path, STUB_CONTAINER_IDS="", STUB_BACKEND_ID="")
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.strip().splitlines()
        assert lines[0].startswith("modulo support bundle 1 ref=MSB-1-")
        assert lines[1].startswith(f"bundle: {out} (")
        assert lines[2] == ("Log tails were withheld because redaction via farna.support.bundle was unavailable (3 service(s)).")
        assert "before sending" in lines[3]

    def test_app_failure_reason_is_recorded_not_swallowed(self, tmp_path: Path) -> None:
        # A running backend whose in-container module is absent: the run still
        # succeeds and the reason names the failing import.
        proc, out = _collect(
            tmp_path,
            STUB_CONTAINER_IDS="cid123",
            STUB_BACKEND_ID="cid123",
            STUB_RUNNING="true",
            STUB_APP_OK="0",
        )
        assert proc.returncode == 0, proc.stderr
        manifest = _member_text(out, "MANIFEST.txt")
        assert "farna.support.bundle failed" in manifest
        assert "No module named farna.support.bundle" in manifest


class TestCredentials:
    def test_env_and_secrets_are_never_copied(self, tmp_path: Path) -> None:
        (tmp_path / ".env").write_text("FERNET_KEY=planted-secret-value\n", encoding="utf-8")
        (tmp_path / "secrets.json").write_text('{"state_hmac_key_hex": "planted-secret-value"}\n', encoding="utf-8")
        proc, out = _collect(tmp_path, STUB_CONTAINER_IDS="", STUB_BACKEND_ID="")
        assert proc.returncode == 0, proc.stderr
        names = _members(out)
        assert ".env" not in names
        assert "secrets.json" not in names
        for name in names:
            assert "planted-secret-value" not in _member_text(out, name)

    def test_no_compose_config_dump_is_collected(self, tmp_path: Path) -> None:
        # `docker compose config` (bare) interpolates env values; only the
        # selector forms may be used.
        proc, _ = _collect(tmp_path, STUB_CONTAINER_IDS="", STUB_BACKEND_ID="")
        assert proc.returncode == 0, proc.stderr
        log = (tmp_path / "docker.log").read_text(encoding="utf-8")
        for line in log.splitlines():
            tokens = line.split()
            if "compose" in tokens and "config" in tokens:
                assert "--services" in tokens or "--volumes" in tokens


class TestLogs:
    def test_repeated_lines_are_collapsed(self, tmp_path: Path) -> None:
        log = tmp_path / "crash-loop.log"
        log.write_text("".join("panic: boom\n" for _ in range(60)) + "final line\n", encoding="utf-8")
        proc, out = _collect(
            tmp_path,
            STUB_CONTAINER_IDS="",
            STUB_BACKEND_ID="",
            STUB_LOGS_FILE=str(log),
            # The module's redaction gate must be reachable for a log member to
            # be included at all; it acts as a pass-through for harmless text.
            STUB_REDACT_STDIN_OK="1",
        )
        assert proc.returncode == 0, proc.stderr
        member = _member_text(out, "docker/logs/backend.log.tail")
        assert "panic: boom [repeated 60 times]" in member
        assert "final line" in member
        assert member.count("panic: boom") == 1

    def test_logs_failure_is_recorded_in_the_member(self, tmp_path: Path) -> None:
        proc, out = _collect(
            tmp_path,
            STUB_CONTAINER_IDS="",
            STUB_BACKEND_ID="",
            STUB_LOGS_FAIL="1",
            STUB_REDACT_STDIN_OK="1",
        )
        assert proc.returncode == 0, proc.stderr
        member = _member_text(out, "docker/logs/backend.log.tail")
        assert "docker compose logs backend failed" in member


PLANTED_GITHUB_TOKEN = "ghp_planted1234567890abcdef"


class TestLogRedaction:
    """Docker-level logs are raw engine output; they must never ship unfiltered."""

    def test_planted_token_never_reaches_the_bundle(self, tmp_path: Path) -> None:
        log = tmp_path / "app.log"
        log.write_text(
            f"starting\nGITHUB_TOKEN={PLANTED_GITHUB_TOKEN}\nready\n",
            encoding="utf-8",
        )
        proc, out = _collect(
            tmp_path,
            STUB_CONTAINER_IDS="",
            STUB_BACKEND_ID="",
            STUB_LOGS_FILE=str(log),
            STUB_REDACT_STDIN_OK="1",
        )
        assert proc.returncode == 0, proc.stderr
        names = _members(out)
        assert "docker/logs/backend.log.tail" in names
        for name in names:
            assert PLANTED_GITHUB_TOKEN not in _member_text(out, name)
        member = _member_text(out, "docker/logs/backend.log.tail")
        assert "[REDACTED:github-token]" in member
        assert "starting" in member
        assert "ready" in member

    def test_unavailable_filter_withholds_the_log_member(self, tmp_path: Path) -> None:
        # Default stub: the one-off container cannot run (image absent). The
        # collector must omit every log member and say why - never ship raw.
        log = tmp_path / "app.log"
        log.write_text(f"GITHUB_TOKEN={PLANTED_GITHUB_TOKEN}\n", encoding="utf-8")
        proc, out = _collect(
            tmp_path,
            STUB_CONTAINER_IDS="",
            STUB_BACKEND_ID="",
            STUB_LOGS_FILE=str(log),
        )
        assert proc.returncode == 0, proc.stderr
        names = _members(out)
        assert not [name for name in names if name.startswith("docker/logs/")]
        manifest = _member_text(out, "MANIFEST.txt")
        assert "log tail withheld" in manifest
        assert "python -m farna.support.bundle --redact-stdin" in manifest
        assert "pull access denied" in manifest
        for name in names:
            assert PLANTED_GITHUB_TOKEN not in _member_text(out, name)
        assert "Log tails were withheld" in proc.stdout

    def test_refused_token_withholds_the_member_and_records_the_reason(self, tmp_path: Path) -> None:
        # The gate itself refused the input (exit non-zero): withhold and note,
        # do not fail the whole run.
        log = tmp_path / "app.log"
        log.write_text(f"GITHUB_TOKEN={PLANTED_GITHUB_TOKEN}\n", encoding="utf-8")
        proc, out = _collect(
            tmp_path,
            STUB_CONTAINER_IDS="",
            STUB_BACKEND_ID="",
            STUB_LOGS_FILE=str(log),
            STUB_REDACT_STDIN_OK="1",
            STUB_REDACT_STDIN_REFUSE="1",
        )
        assert proc.returncode == 0, proc.stderr
        names = _members(out)
        assert not [name for name in names if name.startswith("docker/logs/")]
        manifest = _member_text(out, "MANIFEST.txt")
        assert "log tail withheld" in manifest
        assert "unclassified high-entropy token" in manifest
        for name in names:
            assert PLANTED_GITHUB_TOKEN not in _member_text(out, name)


#: The FULL ``--json`` payload the FAR-2305 module emits after the ref-contract
#: change. Pinned as a whole object (not just the two keys the collector reads)
#: so a rename on either side fails loudly here instead of silently producing an
#: empty ``REF`` and two halves that disagree.
MODULE_JSON_PAYLOAD: dict[str, object] = {
    "bundle_format_version": 7,
    "ref": "MSB-7-20260101T000000Z-deadbeef",
    "ref_line": "modulo support bundle 7 ref=MSB-7-20260101T000000Z-deadbeef",
    "bundle_path": "/tmp/modulo-support.zip",
    "members": [{"name": "version.txt", "bytes": 21}],
    "bytes": 21,
    "redaction": {"exact": 1, "structural": 0, "patterns": 2},
    "missing": [],
    "truncated_log_members": [],
}


def _app_bundle(tmp_path: Path, *, format_version: int = 7) -> Path:
    """The zip the in-container module would produce (FAR-2305 contract)."""
    path = tmp_path / "module-bundle.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("MANIFEST.txt", f"Modulo support bundle\nbundle_format_version: {format_version}\n")
        zf.writestr("report.json", json.dumps({"bundle_format_version": format_version, "members": []}))
        zf.writestr("version.txt", "modulo version: 9.9.9\n")
        zf.writestr("logs/backend.log.tail", "app log tail\n")
    return path


class TestAppMerge:
    def _run_with_app(self, tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
        return _collect(
            tmp_path,
            STUB_CONTAINER_IDS="cid123",
            STUB_BACKEND_ID="cid123",
            STUB_RUNNING="true",
            STUB_APP_OK="1",
            STUB_APP_JSON=json.dumps(MODULE_JSON_PAYLOAD),
            STUB_APP_ZIP=str(_app_bundle(tmp_path)),
            STUB_REDACT_STDIN_OK="1",
        )

    def test_module_json_payload_shape_is_pinned(self) -> None:
        # The fixture above is the contract; this test fails if someone edits it
        # to a shape the collector does not read.
        assert set(MODULE_JSON_PAYLOAD) == {
            "bundle_format_version",
            "ref",
            "ref_line",
            "bundle_path",
            "members",
            "bytes",
            "redaction",
            "missing",
            "truncated_log_members",
        }
        assert str(MODULE_JSON_PAYLOAD["ref"]).startswith("MSB-7-")
        assert MODULE_JSON_PAYLOAD["ref_line"] == (f"modulo support bundle 7 ref={MODULE_JSON_PAYLOAD['ref']}")

    def test_composite_ref_is_the_module_ref_when_the_app_half_is_present(self, tmp_path: Path) -> None:
        proc, out = self._run_with_app(tmp_path)
        assert proc.returncode == 0, proc.stderr
        module_ref = str(MODULE_JSON_PAYLOAD["ref"])
        report = json.loads(_member_text(out, "report.json"))
        manifest = _member_text(out, "MANIFEST.txt")
        assert report["ref"] == module_ref
        assert report["ref_line"] == MODULE_JSON_PAYLOAD["ref_line"]
        assert f"ref: {module_ref}" in manifest
        assert f"ref_line: {MODULE_JSON_PAYLOAD['ref_line']}" in manifest
        assert proc.stdout.splitlines()[0] == MODULE_JSON_PAYLOAD["ref_line"]

    def test_app_and_docker_members_share_one_zip(self, tmp_path: Path) -> None:
        proc, out = self._run_with_app(tmp_path)
        assert proc.returncode == 0, proc.stderr
        names = _members(out)
        assert "version.txt" in names
        assert "logs/backend.log.tail" in names
        assert "docker/compose-ps.txt" in names

    def test_module_manifest_and_report_are_preserved_under_app(self, tmp_path: Path) -> None:
        proc, out = self._run_with_app(tmp_path)
        assert proc.returncode == 0, proc.stderr
        names = _members(out)
        assert "app/MANIFEST.txt" in names
        assert "app/report.json" in names
        assert "app/bundle-cli.json" in names

    def test_module_ref_line_and_format_version_win(self, tmp_path: Path) -> None:
        proc, out = self._run_with_app(tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines()[0] == "modulo support bundle 7 ref=MSB-7-20260101T000000Z-deadbeef"
        report = json.loads(_member_text(out, "report.json"))
        assert report["bundle_format_version"] == 7
        assert report["app_bundle"]["collected"] is True

    def test_manifest_lists_both_sources(self, tmp_path: Path) -> None:
        proc, out = self._run_with_app(tmp_path)
        assert proc.returncode == 0, proc.stderr
        manifest = _member_text(out, "MANIFEST.txt")
        assert "missing: []" in manifest
        assert "[app]" in manifest
        assert "[docker]" in manifest

    def test_unsafe_member_paths_are_refused(self, tmp_path: Path) -> None:
        # Zip-slip defence: a member path that escapes the staging dir must not
        # be merged, and the refusal must be recorded rather than silent.
        evil = tmp_path / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("../escaped.txt", "nope\n")
            zf.writestr("version.txt", "modulo version: 9.9.9\n")
        payload = json.dumps({"ref": "MSB-1-x", "bundle_format_version": 1})
        proc, out = _collect(
            tmp_path,
            STUB_CONTAINER_IDS="cid123",
            STUB_BACKEND_ID="cid123",
            STUB_RUNNING="true",
            STUB_APP_OK="1",
            STUB_APP_JSON=payload,
            STUB_APP_ZIP=str(evil),
        )
        assert proc.returncode == 0, proc.stderr
        assert not (tmp_path / "escaped.txt").exists()
        assert "unsafe member paths" in _member_text(out, "MANIFEST.txt")
