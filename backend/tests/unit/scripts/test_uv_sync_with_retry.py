"""Unit tests for ``scripts/uv_sync_with_retry.sh``.

The backend lockfile pins source-archive dependencies (e.g. pytest-bdd) that
``uv sync --frozen`` downloads from GitHub's codeload CDN. A transient 504 on
that CDN failed four pre-deploy jobs at once on main (2026-11-08) before any
test ran, so ``deploy.yml`` routes every dependency install through this
wrapper. The same flake then failed CI's ``backend-lint`` job, so ``ci.yml``
routes its ``uv sync`` invocations through the wrapper too. It next failed the
BDD suite's dependency install (run 42477715006), so ``bdd.yml`` routes its
``uv sync`` through the wrapper as well. These tests pin the retry policy:
transient download errors are retried, deterministic failures are not, and the
attempt count is bounded.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "uv_sync_with_retry.sh"

FAKE_UV = """#!/usr/bin/env bash
count_file="${UV_TEST_COUNT_FILE}"
count=$(( $(cat "${count_file}" 2>/dev/null || echo 0) + 1 ))
echo "${count}" > "${count_file}"
if [ "${count}" -le "${UV_TEST_FAIL_TIMES:-0}" ]; then
  echo "error: Failed to download and build pytest-bdd" >&2
  echo "  cause: HTTP status server error (504 Gateway Timeout)" >&2
  exit 2
fi
if [ "${UV_TEST_ALWAYS_FAIL:-0}" = "1" ]; then
  echo "error: No solution found when resolving dependencies" >&2
  exit 2
fi
exit 0
"""


def _run(
    tmp_path: Path,
    *,
    fail_times: int,
    always_fail: bool = False,
    max_attempts: int = 3,
) -> tuple[subprocess.CompletedProcess[str], int]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(FAKE_UV, encoding="utf-8")
    fake_uv.chmod(0o755)
    count_file = tmp_path / "count"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "UV_TEST_COUNT_FILE": str(count_file),
        "UV_TEST_FAIL_TIMES": str(fail_times),
        "UV_TEST_ALWAYS_FAIL": "1" if always_fail else "0",
        "UV_SYNC_MAX_ATTEMPTS": str(max_attempts),
        "UV_SYNC_BACKOFF_SECONDS": "0",
    }
    result = subprocess.run(  # noqa: S603 - fixed argv, repo script under test
        ["bash", str(SCRIPT), "--frozen"],  # noqa: S607 - bash from PATH in CI
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=60,
    )
    calls = int(count_file.read_text(encoding="utf-8")) if count_file.exists() else 0
    return result, calls


def test_script_parses() -> None:
    check = subprocess.run(  # noqa: S603 - fixed argv, repo script under test
        ["bash", "-n", str(SCRIPT)],  # noqa: S607 - bash from PATH in CI
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert check.returncode == 0
    assert not check.stderr


def test_transient_download_error_is_retried_then_succeeds(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, fail_times=2)
    assert result.returncode == 0, result.stderr
    assert calls == 3


def test_deterministic_failure_is_not_retried(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, fail_times=0, always_fail=True)
    assert result.returncode != 0
    assert calls == 1
    assert "non-transient" in result.stdout


def test_transient_failure_is_bounded_by_max_attempts(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, fail_times=99, max_attempts=3)
    assert result.returncode != 0
    assert calls == 3
    assert "after 3 attempts" in result.stdout


def _assert_no_raw_uv_sync(workflow_name: str) -> str:
    text = (REPO_ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines()]
    non_comment = [line for line in lines if line and not line.startswith("#")]
    raw_syncs = [line for line in non_comment if "uv sync" in line and "uv_sync_with_retry" not in line]
    assert not raw_syncs, f"{workflow_name} invokes uv sync directly instead of the retry wrapper: {raw_syncs}"
    return text


def test_deploy_workflow_routes_every_uv_sync_through_the_wrapper() -> None:
    deploy = _assert_no_raw_uv_sync("deploy.yml")
    assert deploy.count("uv_sync_with_retry.sh") == 4


def test_ci_workflow_routes_every_uv_sync_through_the_wrapper() -> None:
    ci = _assert_no_raw_uv_sync("ci.yml")
    assert ci.count("uv_sync_with_retry.sh") == 10


def test_bdd_workflow_routes_every_uv_sync_through_the_wrapper() -> None:
    bdd = _assert_no_raw_uv_sync("bdd.yml")
    assert bdd.count("uv_sync_with_retry.sh") == 1
