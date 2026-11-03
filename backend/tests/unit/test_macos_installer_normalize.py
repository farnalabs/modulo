"""FAR-2327: the macOS installer normalize step must honour reusable-workflow call inputs
even when the caller (release-unified) was itself started by workflow_dispatch."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _run_bash(script: str, *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is required to execute workflow scripts")
    return subprocess.run(  # noqa: S603 - fixed bash executable and repository-owned workflow text
        [bash, "--noprofile", "--norc", "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _macos_normalize_script() -> str:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "macos-installer.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["normalize-inputs"]["steps"]
    run = next(step["run"] for step in steps if step.get("id") == "normalize")
    assert isinstance(run, str)
    return run


def _run_macos_normalize(tmp_path: Path, **overrides: str) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("CALL_", "DISPATCH_", "GITHUB_"))}
    # Actions always defines every mapped env var (empty when unset), so default them all to "".
    for name in ("VERSION", "RC_TAG", "REF", "MODE"):
        env[f"CALL_{name}"] = ""
        env[f"DISPATCH_{name}"] = ""
    env["DISPATCH_BUILD_NATIVE"] = ""
    env.update({"GITHUB_OUTPUT": str(out), "GITHUB_REF": "refs/heads/main"})
    env.update(overrides)
    result = _run_bash(_macos_normalize_script(), cwd=tmp_path, env=env)
    outputs = dict(line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line)
    return result, outputs


def test_macos_normalize_uses_call_inputs_when_caller_was_dispatched(tmp_path: Path) -> None:
    result, outputs = _run_macos_normalize(
        tmp_path,
        GITHUB_EVENT_NAME="workflow_dispatch",
        CALL_VERSION="0.3.324",
        CALL_RC_TAG="v0.3.324-rc5",
        CALL_REF="v0.3.324-rc5",
        CALL_MODE="recut",
        DISPATCH_VERSION="",
        DISPATCH_RC_TAG="",
        DISPATCH_REF="",
        DISPATCH_MODE="",
        DISPATCH_BUILD_NATIVE="",
    )
    assert result.returncode == 0, result.stderr
    assert outputs["rc_tag"] == "v0.3.324-rc5"
    assert outputs["mode"] == "recut"
    assert outputs["run_native_build"] == "true"


def test_macos_normalize_direct_dispatch_dry_run_still_works(tmp_path: Path) -> None:
    result, outputs = _run_macos_normalize(
        tmp_path,
        GITHUB_EVENT_NAME="workflow_dispatch",
        CALL_VERSION="0.3.324",
        CALL_RC_TAG="manual",
        CALL_MODE="dry-run",
        DISPATCH_VERSION="0.3.324",
        DISPATCH_RC_TAG="manual",
        DISPATCH_REF="",
        DISPATCH_MODE="dry-run",
        DISPATCH_BUILD_NATIVE="true",
    )
    assert result.returncode == 0, result.stderr
    assert outputs["ref"] == "refs/heads/main"
    assert outputs["run_native_build"] == "true"


def test_macos_normalize_direct_dispatch_rejects_non_dry_run(tmp_path: Path) -> None:
    result, _outputs = _run_macos_normalize(
        tmp_path,
        GITHUB_EVENT_NAME="workflow_dispatch",
        DISPATCH_VERSION="0.3.324",
        DISPATCH_RC_TAG="manual",
        DISPATCH_REF="",
        DISPATCH_MODE="recut",
        DISPATCH_BUILD_NATIVE="false",
    )
    assert result.returncode != 0
    assert "unsupported manual dispatch mode" in result.stderr
