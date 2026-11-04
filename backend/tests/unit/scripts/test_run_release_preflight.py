"""Unit tests for scripts/run_release_preflight.py (Release Preflight Gate FAR-2344)."""

from __future__ import annotations

import json
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest
import yaml

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "run_release_preflight.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/run_release_preflight.py)")

_loader = SourceFileLoader("run_release_preflight", str(script_path))
mod = module_from_spec(spec_from_loader("run_release_preflight", _loader))
sys.modules["run_release_preflight"] = mod
_loader.exec_module(mod)

ReleasePreflightChecker = mod.ReleasePreflightChecker
PreflightCheckResult = mod.PreflightCheckResult


def _make_dummy_repo(tmp_path: Path) -> Path:
    """Create a minimal mock repository structure satisfying file existence checks."""
    (tmp_path / "frontend" / "src" / "lib" / "api").mkdir(parents=True, exist_ok=True)
    schema_file = tmp_path / "frontend" / "src" / "lib" / "api" / "schema.ts"
    schema_file.write_text("export type schema = {};\n", encoding="utf-8")

    (tmp_path / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".github" / "workflows" / "macos-installer.yml").write_text("name: macOS\n", encoding="utf-8")
    (tmp_path / ".github" / "workflows" / "windows-installer.yml").write_text(
        "name: Windows\n# Find-Iscc probe\n", encoding="utf-8"
    )
    (tmp_path / ".github" / "workflows" / "release-unified.yml").write_text(
        "outputs:\n  rc_tag: ${{ steps.rc-output.outputs.rc_tag }}\n"
        "id: rc-output\nrc_tag: ${{ needs.release.outputs.rc_tag }}\n",
        encoding="utf-8",
    )

    (tmp_path / "deploy" / "windows").mkdir(parents=True, exist_ok=True)
    (tmp_path / "deploy" / "windows" / "modulo.iss").write_text(
        "[Setup]\nAppName=Modulo\nAppVersion=0.1.0\n", encoding="utf-8"
    )
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts" / "create-windows-launcher.ps1").write_text("# launcher\n", encoding="utf-8")

    (tmp_path / "deploy" / "docker").mkdir(parents=True, exist_ok=True)
    (tmp_path / "deploy" / "docker" / "Dockerfile.all-in-one").write_text("FROM alpine\n", encoding="utf-8")
    (tmp_path / "backend").mkdir(parents=True, exist_ok=True)
    (tmp_path / "backend" / "Dockerfile").write_text("FROM alpine\n", encoding="utf-8")
    (tmp_path / "backend" / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")
    (tmp_path / "backend" / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "frontend" / "Dockerfile.prod").write_text("FROM alpine\n", encoding="utf-8")
    (tmp_path / "deploy" / "docker" / "runner-opencode.Dockerfile").write_text("FROM alpine\n", encoding="utf-8")

    return tmp_path


# -----------------------------------------------------------------------------
# 1. Schema freshness
# -----------------------------------------------------------------------------
def test_schema_freshness_passes_when_file_exists(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    checker = ReleasePreflightChecker(mode="dry-run", version="0.1.0", repo_root=repo, env={})
    res = checker.check_schema_freshness()
    assert res.passed
    assert "schema.ts" in res.details


def test_schema_freshness_fails_when_file_missing(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    (repo / "frontend" / "src" / "lib" / "api" / "schema.ts").unlink()
    checker = ReleasePreflightChecker(mode="dry-run", version="0.1.0", repo_root=repo, env={})
    res = checker.check_schema_freshness()
    assert not res.passed
    assert "does not exist" in res.details


def test_schema_freshness_in_ci_checks_ci_run_success(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    env = {
        "GITHUB_ACTIONS": "true",
        "GH_TOKEN": "token-123",
        "GITHUB_REPOSITORY": "farnalabs/modulo",
        "CUT_SHA": "abc1234",
    }

    def fake_run(cmd, **kwargs):
        if "run" in cmd and "list" in cmd:
            return 0, json.dumps([{"databaseId": 999}]), ""
        if "api" in cmd and any("jobs" in arg for arg in cmd):
            return 0, json.dumps({"conclusion": "success", "status": "completed"}), ""
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, env=env, run_cmd=fake_run)
    res = checker.check_schema_freshness()
    assert res.passed
    assert "passed" in res.details


def test_schema_freshness_in_ci_fails_when_ci_job_failed(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    env = {
        "GITHUB_ACTIONS": "true",
        "GH_TOKEN": "token-123",
        "GITHUB_REPOSITORY": "farnalabs/modulo",
        "CUT_SHA": "abc1234",
    }

    def fake_run(cmd, **kwargs):
        if "run" in cmd and "list" in cmd:
            return 0, json.dumps([{"databaseId": 999}]), ""
        if "api" in cmd and any("jobs" in arg for arg in cmd):
            return 0, json.dumps({"conclusion": "failure", "status": "completed"}), ""
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, env=env, run_cmd=fake_run)
    res = checker.check_schema_freshness()
    assert not res.passed
    assert "failed" in res.details


def test_schema_freshness_fails_closed_when_ci_query_errors(tmp_path: Path):
    """A failed query is never evidence of absence: it must fail, not pass."""
    repo = _make_dummy_repo(tmp_path)
    env = {
        "GH_TOKEN": "token-123",
        "GITHUB_REPOSITORY": "farnalabs/modulo",
        "GITHUB_SHA": "abc1234",
    }

    def fake_run(cmd, **kwargs):
        if "run" in cmd and "list" in cmd:
            return 1, "", "HTTP 404: Not Found (token lacks actions scope)"
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, env=env, run_cmd=fake_run)
    res = checker.check_schema_freshness()
    assert not res.passed
    assert "Could not query CI runs" in res.details


def test_schema_freshness_passes_in_cut_mode_when_no_ci_run_exists_yet(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    env = {
        "GH_TOKEN": "token-123",
        "GITHUB_REPOSITORY": "farnalabs/modulo",
        "GITHUB_SHA": "abc1234",
    }

    def fake_run(cmd, **kwargs):
        if "run" in cmd and "list" in cmd:
            return 0, "[]", ""
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, env=env, run_cmd=fake_run)
    res = checker.check_schema_freshness()
    assert res.passed
    assert "No CI run found yet" in res.details
    assert "downstream schema-freshness step will wait" in res.details


def test_schema_freshness_passes_when_job_is_in_progress(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    env = {
        "GH_TOKEN": "token-123",
        "GITHUB_REPOSITORY": "farnalabs/modulo",
        "GITHUB_SHA": "abc1234",
    }

    def fake_run(cmd, **kwargs):
        if "run" in cmd and "list" in cmd:
            return 0, json.dumps([{"databaseId": 42}]), ""
        if "api" in cmd and any("jobs" in arg for arg in cmd):
            return 0, json.dumps({"conclusion": None, "status": "in_progress"}), ""
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="recut", version="0.1.0", repo_root=repo, env=env, run_cmd=fake_run)
    res = checker.check_schema_freshness()
    assert res.passed
    assert "in progress" in res.details
    assert "downstream schema-freshness step will wait" in res.details


def test_schema_freshness_passes_when_job_is_queued_or_waiting(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    env = {
        "GH_TOKEN": "token-123",
        "GITHUB_REPOSITORY": "farnalabs/modulo",
        "GITHUB_SHA": "abc1234",
    }

    def fake_run(cmd, **kwargs):
        if "run" in cmd and "list" in cmd:
            return 0, json.dumps([{"databaseId": 42}]), ""
        if "api" in cmd and any("jobs" in arg for arg in cmd):
            return 0, json.dumps({"conclusion": None, "status": "queued"}), ""
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, env=env, run_cmd=fake_run)
    res = checker.check_schema_freshness()
    assert res.passed
    assert "queued" in res.details


def test_schema_freshness_fails_when_job_is_not_green_and_completed(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    env = {
        "GH_TOKEN": "token-123",
        "GITHUB_REPOSITORY": "farnalabs/modulo",
        "GITHUB_SHA": "abc1234",
    }

    def fake_run(cmd, **kwargs):
        if "run" in cmd and "list" in cmd:
            return 0, json.dumps([{"databaseId": 42}]), ""
        if "api" in cmd and any("jobs" in arg for arg in cmd):
            return 0, json.dumps({"conclusion": "cancelled", "status": "completed"}), ""
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="recut", version="0.1.0", repo_root=repo, env=env, run_cmd=fake_run)
    res = checker.check_schema_freshness()
    assert not res.passed
    assert "not green" in res.details
    assert "cancelled" in res.details


def _schema_ci_env() -> dict[str, str]:
    return {
        "GH_TOKEN": "token-123",
        "GITHUB_REPOSITORY": "farnalabs/modulo",
        "GITHUB_SHA": "abc1234",
    }


def _run_id_from_jobs_cmd(cmd) -> str:
    url = next(arg for arg in cmd if arg.endswith("/jobs"))
    return url.rsplit("/", 2)[-2]


def test_schema_freshness_uses_a_green_run_when_latest_is_not_green(tmp_path: Path):
    """A re-run (or docs-only run) must not mask a green Schema freshness job."""
    repo = _make_dummy_repo(tmp_path)
    jobs = {
        "100": {"conclusion": "failure", "status": "completed"},
        "99": {"conclusion": "success", "status": "completed"},
    }

    def fake_run(cmd, **kwargs):
        if "run" in cmd and "list" in cmd:
            return 0, json.dumps([{"databaseId": 100}, {"databaseId": 99}]), ""
        if "api" in cmd and any(arg.endswith("/jobs") for arg in cmd):
            return 0, json.dumps(jobs[_run_id_from_jobs_cmd(cmd)]), ""
        return 0, "", ""

    checker = ReleasePreflightChecker(
        mode="cut", version="0.1.0", repo_root=repo, env=_schema_ci_env(), run_cmd=fake_run
    )
    res = checker.check_schema_freshness()
    assert res.passed
    assert "run 99" in res.details


def test_schema_freshness_skips_runs_without_the_job(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        if "run" in cmd and "list" in cmd:
            return 0, json.dumps([{"databaseId": 100}, {"databaseId": 99}]), ""
        if "api" in cmd and any(arg.endswith("/jobs") for arg in cmd):
            if _run_id_from_jobs_cmd(cmd) == "100":
                return 0, "", ""
            return 0, json.dumps({"conclusion": "success", "status": "completed"}), ""
        return 0, "", ""

    checker = ReleasePreflightChecker(
        mode="cut", version="0.1.0", repo_root=repo, env=_schema_ci_env(), run_cmd=fake_run
    )
    res = checker.check_schema_freshness()
    assert res.passed
    assert "run 99" in res.details


def test_schema_freshness_fails_when_no_run_carries_the_job(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        if "run" in cmd and "list" in cmd:
            return 0, json.dumps(["junk", {}, {"databaseId": ""}, {"databaseId": 7}]), ""
        if "api" in cmd and any(arg.endswith("/jobs") for arg in cmd):
            return 0, "", ""
        return 0, "", ""

    checker = ReleasePreflightChecker(
        mode="cut", version="0.1.0", repo_root=repo, env=_schema_ci_env(), run_cmd=fake_run
    )
    res = checker.check_schema_freshness()
    assert not res.passed
    assert "not found in any CI run" in res.details


def test_schema_freshness_fails_closed_when_job_payload_is_unparseable(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        if "run" in cmd and "list" in cmd:
            return 0, json.dumps([{"databaseId": 7}]), ""
        if "api" in cmd and any(arg.endswith("/jobs") for arg in cmd):
            return 0, "not-json", ""
        return 0, "", ""

    checker = ReleasePreflightChecker(
        mode="cut", version="0.1.0", repo_root=repo, env=_schema_ci_env(), run_cmd=fake_run
    )
    res = checker.check_schema_freshness()
    assert not res.passed
    assert "Could not parse the schema-freshness job" in res.details


def test_schema_freshness_fails_closed_when_jobs_query_errors(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        if "run" in cmd and "list" in cmd:
            return 0, json.dumps([{"databaseId": 7}]), ""
        if "api" in cmd and any(arg.endswith("/jobs") for arg in cmd):
            return 1, "", "HTTP 403: Forbidden"
        return 0, "", ""

    checker = ReleasePreflightChecker(
        mode="cut", version="0.1.0", repo_root=repo, env=_schema_ci_env(), run_cmd=fake_run
    )
    res = checker.check_schema_freshness()
    assert not res.passed
    assert "Could not read the jobs of CI run 7" in res.details
    assert "HTTP 403" in res.details


# -----------------------------------------------------------------------------
# 2. macOS installer inputs
# -----------------------------------------------------------------------------
def test_macos_installer_inputs_valid(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    checker = ReleasePreflightChecker(mode="cut", version="0.3.324", repo_root=repo)
    res = checker.check_macos_installer_inputs()
    assert res.passed
    assert "0.3.324" in res.details


def test_macos_installer_inputs_invalid_mode(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    checker = ReleasePreflightChecker(mode="invalid-mode", version="0.3.324", repo_root=repo)
    res = checker.check_macos_installer_inputs()
    assert not res.passed
    assert "Invalid release mode" in res.details


def test_macos_installer_inputs_invalid_version(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    checker = ReleasePreflightChecker(mode="cut", version="v0.3-beta", repo_root=repo)
    res = checker.check_macos_installer_inputs()
    assert not res.passed
    assert "must be semver X.Y.Z" in res.details


# -----------------------------------------------------------------------------
# 3. Inno Setup availability on Windows
# -----------------------------------------------------------------------------
def test_inno_setup_availability_passes_with_valid_iss_and_workflow(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    checker = ReleasePreflightChecker(mode="dry-run", version="0.1.0", repo_root=repo)
    res = checker.check_inno_setup_availability()
    assert res.passed


def test_inno_setup_availability_fails_when_iss_missing(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    (repo / "deploy" / "windows" / "modulo.iss").unlink()
    checker = ReleasePreflightChecker(mode="dry-run", version="0.1.0", repo_root=repo)
    res = checker.check_inno_setup_availability()
    assert not res.passed
    assert "modulo.iss is missing" in res.details


def test_inno_setup_availability_fails_on_character_code_literal(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    (repo / "deploy" / "windows" / "modulo.iss").write_text("[Setup]\n#123 invalid preprocessor\n", encoding="utf-8")
    checker = ReleasePreflightChecker(mode="dry-run", version="0.1.0", repo_root=repo)
    res = checker.check_inno_setup_availability()
    assert not res.passed
    assert "character-code literal" in res.details


# -----------------------------------------------------------------------------
# 4. GHCR auth / buildx context / tag availability
# -----------------------------------------------------------------------------
def test_ghcr_and_buildx_passes_in_dry_run(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    checker = ReleasePreflightChecker(mode="dry-run", version="0.1.0", repo_root=repo, env={})
    res = checker.check_ghcr_and_buildx()
    assert res.passed


def test_ghcr_and_buildx_fails_when_token_missing_in_ci(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    env = {"GITHUB_ACTIONS": "true"}
    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, env=env)
    res = checker.check_ghcr_and_buildx()
    assert not res.passed
    assert "GH_TOKEN" in res.details


def test_ghcr_and_buildx_fails_when_buildx_unavailable(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    env = {"GITHUB_ACTIONS": "true", "GH_TOKEN": "token-123", "GITHUB_ACTOR": "octocat"}

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "buildx", "version"]:
            return 1, "", "docker: 'buildx' is not a docker command"
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, env=env, run_cmd=fake_run)
    res = checker.check_ghcr_and_buildx()
    assert not res.passed
    assert "docker buildx is unavailable" in res.details
    assert "not a docker command" in res.details


def test_ghcr_and_buildx_fails_when_registry_login_rejected(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    env = {"GITHUB_ACTIONS": "true", "GH_TOKEN": "token-123", "GITHUB_ACTOR": "octocat"}
    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "buildx", "version"]:
            return 0, "github.com/docker/buildx v0.30.0\n", ""
        if cmd[:2] == ["docker", "login"]:
            seen["stdin"] = kwargs.get("stdin_data")
            seen["user"] = cmd[cmd.index("-u") + 1]
            return 1, "", "Error response from daemon: denied"
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, env=env, run_cmd=fake_run)
    res = checker.check_ghcr_and_buildx()
    assert not res.passed
    assert "GHCR authentication failed" in res.details
    # The token is passed on stdin, never as an argument or in the message
    assert seen["stdin"] == "token-123"
    assert seen["user"] == "octocat"
    assert "token-123" not in res.details


def test_ghcr_and_buildx_passes_in_ci_when_buildx_and_login_succeed(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    env = {"GITHUB_ACTIONS": "true", "GH_TOKEN": "token-123", "GITHUB_ACTOR": "octocat"}

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "buildx", "version"]:
            return 0, "github.com/docker/buildx v0.30.0\n", ""
        if cmd[:2] == ["docker", "login"]:
            return 0, "Login Succeeded\n", ""
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="dry-run", version="0.1.0", repo_root=repo, env=env, run_cmd=fake_run)
    res = checker.check_ghcr_and_buildx()
    assert res.passed
    assert "buildx: github.com/docker/buildx v0.30.0" in res.details


def test_ghcr_and_buildx_fails_when_dockerfile_missing(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    (repo / "deploy" / "docker" / "Dockerfile.all-in-one").unlink()
    checker = ReleasePreflightChecker(mode="dry-run", version="0.1.0", repo_root=repo, env={})
    res = checker.check_ghcr_and_buildx()
    assert not res.passed
    assert "Missing release Dockerfile" in res.details


def test_ghcr_and_buildx_fails_when_tag_already_exists_in_cut_mode(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        if "tag" in cmd and "-l" in cmd:
            return 0, "v0.1.0\n", ""
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, env={}, run_cmd=fake_run)
    res = checker.check_ghcr_and_buildx()
    assert not res.passed
    assert "already exist" in res.details


# -----------------------------------------------------------------------------
# 5. rc_tag output defined and propagated
# -----------------------------------------------------------------------------
def test_rc_tag_propagation_dry_run(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    checker = ReleasePreflightChecker(mode="dry-run", version="0.1.0", repo_root=repo)
    res = checker.check_rc_tag_propagation()
    assert res.passed
    assert "v0.1.0-dry-run" in res.details


def test_rc_tag_propagation_promote_fails_without_rc_tag(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="promote", version="0.1.0", repo_root=repo, run_cmd=fake_run)
    res = checker.check_rc_tag_propagation()
    assert not res.passed
    assert "no prior release candidate tags" in res.details


def test_rc_tag_propagation_cut_resolves_next_rc(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        if "tag" in cmd and "-l" in cmd:
            return 0, "v0.1.0-rc1\nv0.1.0-rc2\n", ""
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, run_cmd=fake_run)
    res = checker.check_rc_tag_propagation()
    assert res.passed
    assert "v0.1.0-rc3" in res.details


def test_release_freeze_state_reports_the_freeze_flag(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        return 2, "", ""

    frozen = ReleasePreflightChecker(
        mode="cut", version="0.1.0", freeze=True, repo_root=repo, run_cmd=fake_run
    ).check_release_freeze_state()
    assert frozen.passed
    assert "will be established" in frozen.details

    unfrozen = ReleasePreflightChecker(
        mode="cut", version="0.1.0", freeze=False, repo_root=repo, run_cmd=fake_run
    ).check_release_freeze_state()
    assert unfrozen.passed
    assert "will NOT be paused" in unfrozen.details


# -----------------------------------------------------------------------------
# 6. uv.lock freshness
# -----------------------------------------------------------------------------
def test_uv_lock_freshness_passes_when_lock_clean(tmp_path: Path, monkeypatch):
    repo = _make_dummy_repo(tmp_path)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/uv")

    def fake_run(cmd, **kwargs):
        if "uv" in cmd:
            return 0, "All dependencies resolved\n", ""
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, run_cmd=fake_run)
    res = checker.check_uv_lock_freshness()
    assert res.passed


def test_uv_lock_freshness_fails_when_stale(tmp_path: Path, monkeypatch):
    repo = _make_dummy_repo(tmp_path)
    # Without this the result depends on whether uv happens to be on PATH.
    monkeypatch.setattr(mod.shutil, "which", lambda _name: "/usr/bin/uv")

    def fake_run(cmd, **kwargs):
        if "uv" in cmd:
            return 1, "", "error: lockfile is stale\n"
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, run_cmd=fake_run)
    res = checker.check_uv_lock_freshness()
    assert not res.passed
    assert "stale" in res.details


def test_uv_lock_freshness_skips_dynamic_check_when_uv_absent(tmp_path: Path, monkeypatch):
    repo = _make_dummy_repo(tmp_path)
    monkeypatch.setattr(mod.shutil, "which", lambda _name: None)

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo)
    res = checker.check_uv_lock_freshness()
    assert res.passed
    assert "uv binary not found" in res.details


# -----------------------------------------------------------------------------
# 7. Release-freeze marker state
# -----------------------------------------------------------------------------
def test_release_freeze_state_fails_on_active_hold(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        if any("release-publication-hold" in arg for arg in cmd):
            return 0, "abc refs/heads/release-publication-hold", ""
        return 2, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, run_cmd=fake_run)
    res = checker.check_release_freeze_state()
    assert not res.passed
    assert "release-publication-hold" in res.details


def test_release_freeze_state_fails_on_promote_without_freeze(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        # hold does not exist (rc 2), freeze does not exist (rc 2)
        return 2, "", ""

    checker = ReleasePreflightChecker(mode="promote", version="0.1.0", repo_root=repo, run_cmd=fake_run)
    res = checker.check_release_freeze_state()
    assert not res.passed
    assert "Promotion requires an active" in res.details


def test_release_freeze_state_fails_closed_when_hold_query_errors(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        # 128 is a git transport failure, not "branch absent" (2).
        return 128, "", "fatal: unable to access 'origin': network unreachable"

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, run_cmd=fake_run)
    res = checker.check_release_freeze_state()
    assert not res.passed
    assert "Could not determine 'release-publication-hold' state" in res.details
    assert "network unreachable" in res.details


def test_release_freeze_state_fails_closed_when_freeze_query_errors(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        if any("release-publication-hold" in arg for arg in cmd):
            return 2, "", ""
        return 128, "", "fatal: could not read from remote repository"

    checker = ReleasePreflightChecker(mode="promote", version="0.1.0", repo_root=repo, run_cmd=fake_run)
    res = checker.check_release_freeze_state()
    assert not res.passed
    assert "Could not determine 'release-freeze' state" in res.details
    assert "could not read from remote" in res.details


# -----------------------------------------------------------------------------
# 8. Windows native artifacts payload present
# -----------------------------------------------------------------------------
def test_windows_native_artifacts_dry_run_passes(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    assert not (repo / "windows-native-artifacts.zip").exists()
    checker = ReleasePreflightChecker(mode="dry-run", version="0.1.0", repo_root=repo)
    res = checker.check_windows_native_artifacts()
    assert res.passed
    assert "Windows native artifacts skipped" in res.details
    assert "Beta scope" in res.details


@pytest.mark.parametrize("mode", ["cut", "recut"])
def test_windows_native_artifacts_payload_missing_fails_when_included(tmp_path: Path, mode: str):
    """An included Windows package must fail closed when its payload is absent."""
    repo = _make_dummy_repo(tmp_path)
    checker = ReleasePreflightChecker(mode=mode, version="0.1.0", include_windows=True, repo_root=repo, env={})
    res = checker.check_windows_native_artifacts()
    assert not res.passed
    assert "windows-native-artifacts.zip" in res.details
    assert "missing" in res.details
    assert mode in res.details
    assert "FAR-2338" in res.details
    assert "FAR-2250" in res.details


def test_windows_native_artifacts_payload_missing_is_skipped_for_alpha(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, env={})
    res = checker.check_windows_native_artifacts()
    assert res.passed
    assert "include_windows=false" in res.details
    assert "Beta scope" in res.details


def test_windows_native_artifacts_non_dry_run_passes_when_zip_exists(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    (repo / "windows-native-artifacts.zip").write_bytes(b"dummy zip content")
    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", include_windows=True, repo_root=repo)
    res = checker.check_windows_native_artifacts()
    assert res.passed
    assert "present locally" in res.details


# -----------------------------------------------------------------------------
# 9. Azure Trusted Signing secrets presence
# -----------------------------------------------------------------------------
def test_azure_signing_secrets_dry_run_passes_even_if_missing(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    checker = ReleasePreflightChecker(mode="dry-run", version="0.1.0", repo_root=repo, env={})
    res = checker.check_azure_signing_secrets()
    assert res.passed
    assert "Dry-run mode" in res.details


@pytest.mark.parametrize("mode", ["cut", "recut"])
def test_azure_signing_secrets_cut_and_recut_fail_when_missing(tmp_path: Path, mode: str):
    repo = _make_dummy_repo(tmp_path)
    checker = ReleasePreflightChecker(mode=mode, version="0.1.0", repo_root=repo, env={})
    res = checker.check_azure_signing_secrets()
    assert not res.passed
    assert "Missing required release secret(s)" in res.details
    assert "BUNDLE_MANIFEST_SIGNING_KEY" in res.details
    assert "MODULO_RELEASE_PR_TOKEN" in res.details


def test_azure_signing_secrets_cut_fails_when_only_azure_secrets_missing(tmp_path: Path):
    """A cut needs the Azure Trusted Signing credentials the Windows sign job uses (FAR-2338)."""
    repo = _make_dummy_repo(tmp_path)
    env = {
        "BUNDLE_MANIFEST_SIGNING_KEY": "key-123",
        "MODULO_RELEASE_PR_TOKEN": "token-123",
    }
    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, env=env)
    res = checker.check_azure_signing_secrets()
    assert not res.passed
    assert "Missing required release secret(s)" in res.details
    assert "AZURE_CLIENT_ID" in res.details


def test_azure_signing_secrets_recut_fails_when_only_azure_secrets_missing(tmp_path: Path):
    """A recut is a full re-release: the Windows sign job runs and needs Azure credentials."""
    repo = _make_dummy_repo(tmp_path)
    env = {
        "BUNDLE_MANIFEST_SIGNING_KEY": "key-123",
        "MODULO_RELEASE_PR_TOKEN": "token-123",
    }
    checker = ReleasePreflightChecker(mode="recut", version="0.1.0", repo_root=repo, env=env)
    res = checker.check_azure_signing_secrets()
    assert not res.passed
    assert "Missing required release secret(s)" in res.details
    assert "AZURE_CLIENT_ID" in res.details


def test_azure_signing_secrets_promote_passes_without_azure_secrets(tmp_path: Path):
    """Promote does not build or sign a Windows installer, so Azure credentials are not required."""
    repo = _make_dummy_repo(tmp_path)
    env = {
        "BUNDLE_MANIFEST_SIGNING_KEY": "key-123",
        "MODULO_RELEASE_PR_TOKEN": "token-123",
    }
    checker = ReleasePreflightChecker(mode="promote", version="0.1.0", repo_root=repo, env=env)
    res = checker.check_azure_signing_secrets()
    assert res.passed


def test_azure_signing_secrets_cut_passes_when_present(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)
    env = {
        "AZURE_CLIENT_ID": "id-123",
        "AZURE_TENANT_ID": "tenant-123",
        "AZURE_SUBSCRIPTION_ID": "sub-123",
        "BUNDLE_MANIFEST_SIGNING_KEY": "key-123",
        "MODULO_RELEASE_PR_TOKEN": "token-123",
    }
    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, env=env)
    res = checker.check_azure_signing_secrets()
    assert res.passed
    # Secrets must be redacted
    assert "id-123" not in res.details
    assert "redacted" in res.details


# -----------------------------------------------------------------------------
# 10. Branch merge-conflict check against main
# -----------------------------------------------------------------------------
def test_merge_conflicts_clean_when_head_matches_baseline(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd and "origin/main" in cmd:
            return 0, "111111\n", ""
        if "merge-base" in cmd:
            return 0, "111111\n", ""
        if "rev-parse" in cmd and "HEAD" in cmd:
            return 0, "222222\n", ""
        if "merge-tree" in cmd:
            # First line is tree OID, no conflicted paths
            return 0, "treeoid123\n", ""
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, run_cmd=fake_run)
    res = checker.check_merge_conflicts()
    assert res.passed


def test_merge_conflicts_clean_branch_with_divergent_commits(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd and "origin/main" in cmd:
            return 0, "111111\n", ""
        if "merge-base" in cmd:
            return 0, "000000\n", ""
        if "rev-parse" in cmd and "HEAD" in cmd:
            return 0, "222222\n", ""
        if "merge-tree" in cmd:
            return 0, "treeoid123\n", ""
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, run_cmd=fake_run)
    res = checker.check_merge_conflicts()
    assert res.passed


def test_merge_conflicts_detected_via_write_tree(tmp_path: Path):
    """The real git path: --write-tree exits 1 and prints the conflicted paths."""

    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd and "origin/main" in cmd:
            return 0, "111111\n", ""
        if "merge-base" in cmd:
            return 0, "000000\n", ""
        if "rev-parse" in cmd and "HEAD" in cmd:
            return 0, "222222\n", ""
        if "merge-tree" in cmd and "--write-tree" in cmd:
            # Tree OID, then conflicted paths, a blank line, then messages.
            return (
                1,
                "treeoid123\nbackend/pyproject.toml\n\nAuto-merging backend/pyproject.toml\n"
                "CONFLICT (content): Merge conflict in backend/pyproject.toml\n",
                "",
            )
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, run_cmd=fake_run)
    res = checker.check_merge_conflicts()
    assert not res.passed
    assert "Merge conflicts detected" in res.details
    assert "backend/pyproject.toml" in res.details


def test_merge_conflicts_detected_without_path_names(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd and "origin/main" in cmd:
            return 0, "111111\n", ""
        if "merge-base" in cmd:
            return 0, "000000\n", ""
        if "rev-parse" in cmd and "HEAD" in cmd:
            return 0, "222222\n", ""
        if "merge-tree" in cmd and "--write-tree" in cmd:
            return 1, "treeoid123\n", ""
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, run_cmd=fake_run)
    res = checker.check_merge_conflicts()
    assert not res.passed
    assert "Merge conflicts detected" in res.details


def test_merge_conflicts_fails_closed_when_merge_tree_errors(tmp_path: Path):
    """A git error must never be reported as a clean branch."""

    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd and "origin/main" in cmd:
            return 0, "111111\n", ""
        if "merge-base" in cmd:
            return 0, "000000\n", ""
        if "rev-parse" in cmd and "HEAD" in cmd:
            return 0, "222222\n", ""
        if "merge-tree" in cmd and "--write-tree" in cmd:
            return 129, "", "error: unknown option `write-tree'"
        if "merge-tree" in cmd:
            return 128, "", "fatal: not a git repository"
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, run_cmd=fake_run)
    res = checker.check_merge_conflicts()
    assert not res.passed
    assert "Unable to determine merge state" in res.details
    assert "not a git repository" in res.details


def test_merge_conflicts_falls_back_to_three_arg_form_on_clean_merge(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd and "origin/main" in cmd:
            return 0, "111111\n", ""
        if "merge-base" in cmd:
            return 0, "000000\n", ""
        if "rev-parse" in cmd and "HEAD" in cmd:
            return 0, "222222\n", ""
        if "merge-tree" in cmd and "--write-tree" in cmd:
            return 129, "", "error: unknown option `write-tree'"
        if "merge-tree" in cmd:
            return 0, "merged\n", ""
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, run_cmd=fake_run)
    res = checker.check_merge_conflicts()
    assert res.passed


def test_merge_conflicts_detected_in_three_arg_fallback(tmp_path: Path):
    repo = _make_dummy_repo(tmp_path)

    def fake_run(cmd, **kwargs):
        if "rev-parse" in cmd and "origin/main" in cmd:
            return 0, "111111\n", ""
        if "merge-base" in cmd:
            return 0, "000000\n", ""
        if "rev-parse" in cmd and "HEAD" in cmd:
            return 0, "222222\n", ""
        if "merge-tree" in cmd and "--write-tree" in cmd:
            return 129, "", "error: unknown option `write-tree'"
        if "merge-tree" in cmd:
            return 0, "some output\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> main\n", ""
        return 0, "", ""

    checker = ReleasePreflightChecker(mode="cut", version="0.1.0", repo_root=repo, run_cmd=fake_run)
    res = checker.check_merge_conflicts()
    assert not res.passed
    assert "3-way merge" in res.details


# -----------------------------------------------------------------------------
# Acceptance criterion: Multiple broken checks report ALL failures in one pass
# -----------------------------------------------------------------------------
def test_run_all_checks_aggregates_multiple_failures_in_single_run(tmp_path: Path):
    """FAR-2344 core acceptance criterion:

    Intentionally breaking two checks reports BOTH failures in a single run.
    """
    repo = _make_dummy_repo(tmp_path)
    # 1. Break schema freshness by deleting schema.ts
    (repo / "frontend" / "src" / "lib" / "api" / "schema.ts").unlink()
    # 2. Break Inno setup by deleting modulo.iss
    (repo / "deploy" / "windows" / "modulo.iss").unlink()

    checker = ReleasePreflightChecker(mode="dry-run", version="0.1.0", repo_root=repo)
    results = checker.run_all_checks()

    # Total checks must be 10
    assert len(results) == 10

    failed = [r for r in results if not r.passed]
    failed_names = [r.name for r in failed]

    assert "Schema freshness" in failed_names
    assert "Inno Setup availability on Windows" in failed_names

    # Check that other checks still executed and passed!
    passed_names = [r.name for r in results if r.passed]
    assert "macOS installer inputs" in passed_names
    assert "rc_tag output defined and propagated" in passed_names


def test_format_markdown_summary_contains_all_checks_and_details():
    results = [
        PreflightCheckResult("Check 1", True, "Details 1"),
        PreflightCheckResult("Check 2", False, "Details 2 with | pipe"),
    ]
    summary = ReleasePreflightChecker.format_markdown_summary(results)
    assert "## Release Preflight Gate Summary" in summary
    assert "| Check 1 | ✅ PASS | Details 1 |" in summary
    assert "| Check 2 | ❌ FAIL | Details 2 with \\| pipe |" in summary
    assert "Preflight Gate FAILED" in summary
    assert "Check 2" in summary


def test_main_exits_one_on_failure(tmp_path: Path, capsys):
    repo = _make_dummy_repo(tmp_path)
    # Break an input
    summary_file = tmp_path / "summary.md"
    exit_code = mod.main(
        [
            "--mode",
            "invalid_mode",
            "--version",
            "0.1.0",
            "--repo-root",
            str(repo),
            "--summary-file",
            str(summary_file),
        ]
    )
    assert exit_code == 1
    assert summary_file.exists()
    content = summary_file.read_text(encoding="utf-8")
    assert "Preflight Gate FAILED" in content
    err = capsys.readouterr().err
    assert "PREFLIGHT FAILED" in err


def test_safe_summary_path_allows_plain_and_absolute_paths():
    assert mod._safe_summary_path("summary.md") == "summary.md"
    assert mod._safe_summary_path("/tmp/step_summary_abc-123") == "/tmp/step_summary_abc-123"


def test_safe_summary_path_rejects_empty_traversal_and_unsafe_characters():
    assert mod._safe_summary_path("") is None
    assert mod._safe_summary_path("../../etc/passwd") is None
    assert mod._safe_summary_path("/tmp/../escape.md") is None
    assert mod._safe_summary_path("/tmp/bad name.md") is None
    assert mod._safe_summary_path("/tmp/bad\nname.md") is None


def test_safe_summary_path_accepts_windows_absolute_paths():
    """A Windows runner's ``$GITHUB_STEP_SUMMARY`` is a backslash path.

    ``open()`` accepts ``/`` on Windows, so the path is normalised rather than
    rejected (the pre-existing behaviour refused to write the step summary on
    every Windows host).
    """
    assert mod._safe_summary_path("C:\\Users\\runneradmin\\step_summary.md") == "C:/Users/runneradmin/step_summary.md"
    assert mod._safe_summary_path("C:/Users/runneradmin/step_summary.md") == "C:/Users/runneradmin/step_summary.md"


def test_safe_summary_path_rejects_windows_traversal_and_unsafe_characters():
    assert mod._safe_summary_path("C:\\Users\\..\\..\\etc\\passwd") is None
    assert mod._safe_summary_path("\\tmp\\..\\escape.md") is None
    assert mod._safe_summary_path("C:\\tmp\\bad name.md") is None
    assert mod._safe_summary_path("C:relative.md") is None


def test_main_refuses_traversal_summary_path(tmp_path: Path, capsys):
    repo = _make_dummy_repo(tmp_path)
    write_target = tmp_path / "inside.md"
    (tmp_path / "nested").mkdir()
    exit_code = mod.main(
        [
            "--mode",
            "invalid_mode",
            "--version",
            "0.1.0",
            "--repo-root",
            str(repo),
            "--summary-file",
            f"{tmp_path}/nested/../inside.md",
        ]
    )
    assert exit_code == 1
    assert not write_target.exists()
    err = capsys.readouterr().err
    assert "refusing to write summary to unsafe path" in err


def test_main_refuses_summary_path_with_disallowed_characters(tmp_path: Path, capsys):
    repo = _make_dummy_repo(tmp_path)
    summary_file = tmp_path / "bad name.md"
    exit_code = mod.main(
        [
            "--mode",
            "invalid_mode",
            "--version",
            "0.1.0",
            "--repo-root",
            str(repo),
            "--summary-file",
            str(summary_file),
        ]
    )
    assert exit_code == 1
    assert not summary_file.exists()
    err = capsys.readouterr().err
    assert "refusing to write summary to unsafe path" in err


# -----------------------------------------------------------------------------
# Workflow wiring: the preflight job must gate the release-producing jobs
# -----------------------------------------------------------------------------
def _release_workflow() -> dict:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / ".github" / "workflows" / "release-unified.yml"
        if candidate.exists():
            return yaml.safe_load(candidate.read_text(encoding="utf-8"))
    raise RuntimeError("Could not find .github/workflows/release-unified.yml")


def test_preflight_job_exists_and_runs_on_ubicloud():
    jobs = _release_workflow()["jobs"]
    assert "preflight" in jobs
    assert jobs["preflight"]["runs-on"] == "ubicloud-standard-2"


def test_preflight_job_runs_the_aggregate_checker():
    steps = _release_workflow()["jobs"]["preflight"]["steps"]
    runs = " ".join(step.get("run", "") for step in steps)
    assert "scripts/run_release_preflight.py" in runs


def test_preflight_passes_include_windows_to_the_real_checker():
    step = next(
        s for s in _release_workflow()["jobs"]["preflight"]["steps"] if "run_release_preflight.py" in s.get("run", "")
    )
    assert step["env"]["RELEASE_INCLUDE_WINDOWS"] == "${{ inputs.include_windows }}"
    assert 'windows_arg="--include-windows"' in step["run"]
    assert "$windows_arg" in step["run"]


def test_include_windows_default_is_alpha_safe():
    workflow = _release_workflow()
    assert workflow["on"]["workflow_dispatch"]["inputs"]["include_windows"]["default"] is False
    assert workflow["on"]["workflow_dispatch"]["inputs"]["include_windows"]["description"].endswith("(Beta scope)")


def test_preflight_passes_secrets_but_never_prints_them():
    step = next(
        s for s in _release_workflow()["jobs"]["preflight"]["steps"] if "run_release_preflight.py" in s.get("run", "")
    )
    env = step["env"]
    for secret_name in (
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "AZURE_SUBSCRIPTION_ID",
        "BUNDLE_MANIFEST_SIGNING_KEY",
        "MODULO_RELEASE_PR_TOKEN",
    ):
        assert env[secret_name] == f"${{{{ secrets.{secret_name} }}}}"
    # The run block must reference the environment, never interpolate a secret.
    assert "secrets." not in step["run"]


def test_release_producing_jobs_depend_on_preflight():
    jobs = _release_workflow()["jobs"]
    for job_id in ("dry-run", "dry-run-bundle", "release", "produce-sbom"):
        needs = jobs[job_id]["needs"]
        needs_list = [needs] if isinstance(needs, str) else list(needs)
        assert "preflight" in needs_list, f"{job_id} does not depend on preflight"
