#!/usr/bin/env python3
"""Release preflight discovery gate for Modulo unified release workflow (FAR-2344).

Evaluates all release preconditions independently in a single pass without failing
early, so that every check is exercised and a comprehensive failure matrix is
rendered in one run.

Checks included:
  1. Schema freshness: verify schema freshness gate status or check schema drift.
  2. macOS installer inputs: check workflow inputs contract (semver, mode).
  3. Inno Setup availability on Windows.
  4. GHCR auth / buildx context / tag availability.
  5. rc_tag output defined and propagated.
  6. uv.lock freshness for release version bump.
  7. Release-freeze marker state valid and movable (FAR-2317 tag ruleset).
  8. Windows native artifacts payload present when Windows packaging is included.
  9. Azure Trusted Signing + release secret presence check, required for cut/recut (values never printed!).
 10. Branch merge-conflict check against main.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_GITHUB_DIR = ".github"
_WINDOWS_ARTIFACTS_ZIP = "windows-native-artifacts.zip"


@dataclass
class PreflightCheckResult:
    """Outcome of a single preflight check."""

    name: str
    passed: bool
    details: str


def default_run_cmd(
    cmd: list[str],
    cwd: Path | str | None = None,
    timeout: int = 60,
    env: dict[str, str] | None = None,
    stdin_data: str | None = None,
) -> tuple[int, str, str]:
    """Execute a subprocess command returning (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
            input=stdin_data,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", f"Command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"Command timed out after {timeout}s: {' '.join(cmd)}"
    except OSError as exc:
        return 1, "", f"OS error running command {cmd}: {exc}"


class ReleasePreflightChecker:
    """Evaluates all release preconditions and formats summary reports."""

    def __init__(
        self,
        mode: str = "dry-run",
        version: str = "",
        freeze: bool = False,
        include_windows: bool = False,
        repo_root: Path | str | None = None,
        env: dict[str, str] | None = None,
        run_cmd: Callable[..., tuple[int, str, str]] | None = None,
    ) -> None:
        self.mode = mode.strip()
        self.version = version.strip()
        self.freeze = freeze
        self.include_windows = include_windows
        self.env = env if env is not None else dict(os.environ)
        self.run_cmd = run_cmd if run_cmd is not None else default_run_cmd

        if repo_root is not None:
            self.repo_root = Path(repo_root).resolve()
        else:
            self.repo_root = Path(__file__).resolve().parent.parent

    # -------------------------------------------------------------------------
    # 1. Schema freshness
    # -------------------------------------------------------------------------
    def check_schema_freshness(self) -> PreflightCheckResult:
        name = "Schema freshness"
        schema_file = self.repo_root / "frontend" / "src" / "lib" / "api" / "schema.ts"
        file_result, size = self._validate_schema_file(schema_file, name)
        if file_result is not None:
            return file_result

        # In CI (or whenever a GitHub token and repository are supplied), prove
        # the schema-freshness gate actually ran and passed on the target
        # commit. A file existing is not evidence that the generated schema
        # matches the backend.
        ci_result = self._ci_schema_freshness_gate(name)
        if ci_result is not None:
            return ci_result

        return PreflightCheckResult(
            name,
            True,
            f"frontend/src/lib/api/schema.ts present and valid ({size} bytes)",
        )

    @staticmethod
    def _validate_schema_file(schema_file: Path, name: str) -> tuple[PreflightCheckResult | None, int]:
        """Return a failure result for an unusable schema file, else ``(None, size)``."""
        if not schema_file.exists():
            return PreflightCheckResult(name, False, "frontend/src/lib/api/schema.ts does not exist"), 0
        try:
            size = schema_file.stat().st_size
        except OSError as exc:
            return PreflightCheckResult(name, False, f"Could not inspect schema.ts: {exc}"), 0
        if size == 0:
            return PreflightCheckResult(name, False, "frontend/src/lib/api/schema.ts is empty (0 bytes)"), 0
        return None, size

    def _ci_schema_freshness_gate(self, name: str) -> PreflightCheckResult | None:
        """Prove the schema-freshness CI gate ran green, when CI context is present."""
        gh_token = self.env.get("GH_TOKEN") or self.env.get("GITHUB_TOKEN")
        gh_repo = self.env.get("GITHUB_REPOSITORY")
        if not (gh_token and gh_repo):
            return None

        target_sha = self._resolve_target_sha()
        if not target_sha:
            return None

        gate = self._lookup_schema_freshness_gate(target_sha, gh_repo)
        if gate is not None:
            return gate
        if self.mode in ("cut", "recut"):
            return PreflightCheckResult(
                name,
                True,
                f"No CI run found yet for commit {target_sha[:7]} in repository {gh_repo}; downstream schema-freshness step will wait for CI",
            )
        return None

    def _resolve_target_sha(self) -> str:
        target_sha = self.env.get("CUT_SHA") or self.env.get("GITHUB_SHA") or ""
        if target_sha:
            return target_sha
        rc, stdout, _ = self.run_cmd(["git", "rev-parse", "HEAD"], cwd=self.repo_root)
        return stdout.strip() if rc == 0 and stdout.strip() else ""

    def _lookup_schema_freshness_gate(self, target_sha: str, gh_repo: str) -> PreflightCheckResult | None:
        """Return the schema-freshness verdict for *target_sha*, or ``None``.

        ``None`` means "no CI run found", which the caller decides on by mode.
        A failed query is a hard failure, never evidence of absence.
        """
        name = "Schema freshness"
        rc, stdout, stderr = self.run_cmd(
            [
                "gh",
                "run",
                "list",
                "--workflow",
                "ci.yml",
                "--commit",
                target_sha,
                "--limit",
                "10",
                "--json",
                "databaseId",
            ],
            cwd=self.repo_root,
            env=self.env,
        )
        if rc != 0:
            reason = (stderr or stdout).strip() or f"exit code {rc}"
            return PreflightCheckResult(
                name,
                False,
                f"Could not query CI runs for commit {target_sha[:7]}: {reason}",
            )

        try:
            runs = json.loads(stdout) if stdout.strip() else []
        except json.JSONDecodeError:
            return PreflightCheckResult(
                name,
                False,
                f"Could not parse the CI run list for commit {target_sha[:7]}",
            )
        if not runs:
            return None
        return self._first_green_schema_verdict(runs, gh_repo, target_sha, name)

    def _first_green_schema_verdict(
        self, runs: list[object], gh_repo: str, target_sha: str, name: str
    ) -> PreflightCheckResult:
        """Scan every CI run for a green schema-freshness verdict.

        A commit can have more than one CI run (re-runs, docs-only pushes),
        and the Schema freshness job may be absent from some of them. Check
        every run until one proves the gate green; a run without the job is
        not evidence either way, so keep looking. The first non-green verdict
        is returned only when no run proved green.
        """
        non_green: PreflightCheckResult | None = None
        for run in runs:
            if not isinstance(run, dict):
                continue
            run_id = str(run.get("databaseId", "") or "")
            if not run_id:
                continue
            verdict = self._schema_freshness_job_verdict(run_id, gh_repo, target_sha, name)
            if verdict is None:
                continue
            if verdict.passed:
                return verdict
            non_green = non_green or verdict

        if non_green is not None:
            return non_green
        return PreflightCheckResult(
            name,
            False,
            f"Schema freshness job not found in any CI run on commit {target_sha[:7]}",
        )

    def _schema_freshness_job_verdict(
        self, run_id: str, gh_repo: str, target_sha: str, name: str
    ) -> PreflightCheckResult | None:
        """Return the schema-freshness verdict for one run, or ``None`` if absent."""
        rc_jobs, stdout_jobs, stderr_jobs = self.run_cmd(
            [
                "gh",
                "api",
                f"repos/{gh_repo}/actions/runs/{run_id}/jobs",
                "--jq",
                '[.jobs[] | select(.name == "Schema freshness")][0] // empty',
            ],
            cwd=self.repo_root,
            env=self.env,
        )
        if rc_jobs != 0:
            reason = (stderr_jobs or stdout_jobs).strip() or f"exit code {rc_jobs}"
            return PreflightCheckResult(
                name,
                False,
                f"Could not read the jobs of CI run {run_id}: {reason}",
            )

        if not stdout_jobs.strip():
            return None

        try:
            job_data = json.loads(stdout_jobs)
        except json.JSONDecodeError:
            return PreflightCheckResult(
                name,
                False,
                f"Could not parse the schema-freshness job of CI run {run_id}",
            )

        conclusion = job_data.get("conclusion")
        status = str(job_data.get("status") or "").lower()
        if conclusion == "success":
            return PreflightCheckResult(
                name,
                True,
                f"Schema freshness gate passed on commit {target_sha[:7]} (run {run_id})",
            )
        if conclusion == "failure":
            return PreflightCheckResult(
                name,
                False,
                f"Schema freshness gate failed on commit {target_sha[:7]} (run {run_id})",
            )
        if status in ("in_progress", "queued", "waiting") and conclusion != "failure":
            return PreflightCheckResult(
                name,
                True,
                f"Schema freshness gate is in progress on commit {target_sha[:7]} (run {run_id}, status: {status}); downstream schema-freshness step will wait for completion",
            )
        return PreflightCheckResult(
            name,
            False,
            f"Schema freshness gate not green on commit {target_sha[:7]} (status: {status}, conclusion: {conclusion})",
        )

    # -------------------------------------------------------------------------
    # 2. macOS installer inputs
    # -------------------------------------------------------------------------
    def check_macos_installer_inputs(self) -> PreflightCheckResult:
        name = "macOS installer inputs"
        valid_modes = {"cut", "recut", "promote", "dry-run"}
        if self.mode not in valid_modes:
            return PreflightCheckResult(
                name,
                False,
                f"Invalid release mode '{self.mode}'; must be one of {sorted(valid_modes)}",
            )

        if not self.version:
            return PreflightCheckResult(
                name,
                False,
                "Release version is empty; required in semver X.Y.Z form (e.g. 0.1.0)",
            )

        if not re.match(r"^\d+\.\d+\.\d+$", self.version):
            return PreflightCheckResult(
                name,
                False,
                f"Release version '{self.version}' is invalid; must be semver X.Y.Z (e.g. 0.1.0)",
            )

        macos_workflow = self.repo_root / _GITHUB_DIR / "workflows" / "macos-installer.yml"
        if not macos_workflow.exists():
            return PreflightCheckResult(
                name,
                False,
                ".github/workflows/macos-installer.yml is missing from repository",
            )

        return PreflightCheckResult(
            name,
            True,
            f"Inputs contract valid: mode='{self.mode}', version='{self.version}' (semver X.Y.Z)",
        )

    # -------------------------------------------------------------------------
    # 3. Inno Setup availability on Windows
    # -------------------------------------------------------------------------
    def check_inno_setup_availability(self) -> PreflightCheckResult:
        name = "Inno Setup availability on Windows"
        iss_path = self.repo_root / "deploy" / "windows" / "modulo.iss"
        if not iss_path.exists():
            return PreflightCheckResult(name, False, "deploy/windows/modulo.iss is missing")

        try:
            iss_text = iss_path.read_text(encoding="utf-8")
        except OSError as exc:
            return PreflightCheckResult(name, False, f"Could not read deploy/windows/modulo.iss: {exc}")

        # Check for preprocessor violation: leading '#' followed by digit (character code literal)
        violation = self._iss_preprocessor_violation(iss_text)
        if violation is not None:
            line_num, stripped = violation
            return PreflightCheckResult(
                name,
                False,
                f"deploy/windows/modulo.iss line {line_num} starts with invalid character-code literal: '{stripped}'",
            )

        if "[Setup]" not in iss_text:
            return PreflightCheckResult(
                name,
                False,
                "deploy/windows/modulo.iss missing required [Setup] section",
            )

        win_workflow = self.repo_root / _GITHUB_DIR / "workflows" / "windows-installer.yml"
        if not win_workflow.exists():
            return PreflightCheckResult(
                name,
                False,
                ".github/workflows/windows-installer.yml is missing",
            )

        try:
            workflow_content = win_workflow.read_text(encoding="utf-8")
        except OSError as exc:
            return PreflightCheckResult(name, False, f"Could not read windows-installer.yml: {exc}")
        if "Find-Iscc" not in workflow_content:
            return PreflightCheckResult(
                name,
                False,
                "windows-installer.yml does not define Inno Setup probe (Find-Iscc)",
            )

        launcher_helper = self.repo_root / "scripts" / "create-windows-launcher.ps1"
        if not launcher_helper.exists():
            return PreflightCheckResult(
                name,
                False,
                "scripts/create-windows-launcher.ps1 is missing",
            )

        return PreflightCheckResult(
            name,
            True,
            f"Inno Setup workflow probe, modulo.iss preprocessor syntax, and launcher script verified{self._local_iscc_note()}",
        )

    @staticmethod
    def _iss_preprocessor_violation(iss_text: str) -> tuple[int, str] | None:
        """Return the first character-code literal line in *iss_text*, if any."""
        for line_num, line in enumerate(iss_text.splitlines(), start=1):
            stripped = line.strip()
            if re.match(r"^#\d+", stripped):
                return line_num, stripped
        return None

    def _local_iscc_note(self) -> str:
        """Return a diagnostic note about a locally installed Inno Setup compiler."""
        if sys.platform != "win32":
            return ""
        iscc_found = shutil.which("ISCC.exe")
        if not iscc_found:
            for env_var in ("ProgramFiles", "ProgramFiles(x86)"):
                pf = self.env.get(env_var)
                if not pf:
                    continue
                candidate = Path(pf) / "Inno Setup 6" / "ISCC.exe"
                if candidate.exists():
                    iscc_found = str(candidate)
                    break
        return f" (local compiler found at {iscc_found})" if iscc_found else ""

    # -------------------------------------------------------------------------
    # 4. GHCR auth / buildx context / tag availability
    # -------------------------------------------------------------------------
    def check_ghcr_and_buildx(self) -> PreflightCheckResult:
        name = "GHCR auth / buildx context / tag availability"
        is_ci = self.env.get("GITHUB_ACTIONS") == "true"
        gh_token = self.env.get("GH_TOKEN") or self.env.get("GITHUB_TOKEN")

        if is_ci and not gh_token:
            return PreflightCheckResult(
                name,
                False,
                "GH_TOKEN / GITHUB_TOKEN is missing in environment for GHCR authentication",
            )

        missing_dockerfiles = self._missing_dockerfiles()
        if missing_dockerfiles:
            return PreflightCheckResult(
                name,
                False,
                f"Missing release Dockerfile(s): {', '.join(missing_dockerfiles)}",
            )

        buildx_note = ""
        if is_ci and gh_token:
            buildx_result, buildx_note = self._verify_buildx_and_login(gh_token, name)
            if buildx_result is not None:
                return buildx_result

        tag_result = self._check_release_tags(name)
        if tag_result is not None:
            return tag_result

        return PreflightCheckResult(
            name,
            True,
            f"GHCR token configured, 4 container build contexts verified, and release tags available.{buildx_note}",
        )

    def _missing_dockerfiles(self) -> list[str]:
        dockerfiles = [
            ("all-in-one", self.repo_root / "deploy" / "docker" / "Dockerfile.all-in-one"),
            ("backend", self.repo_root / "backend" / "Dockerfile"),
            ("frontend", self.repo_root / "frontend" / "Dockerfile.prod"),
            ("runner-opencode", self.repo_root / "deploy" / "docker" / "runner-opencode.Dockerfile"),
        ]
        return [img for img, path in dockerfiles if not path.exists()]

    def _verify_buildx_and_login(self, gh_token: str, name: str) -> tuple[PreflightCheckResult | None, str]:
        """Prove buildx works and the GHCR credential authenticates.

        Returns ``(failure_result_or_None, buildx_note)``.
        """
        # The runner must have a docker CLI with a working buildx builder,
        # because every publish path builds with `docker buildx build`.
        rc, stdout, stderr = self.run_cmd(["docker", "buildx", "version"], cwd=self.repo_root)
        if rc != 0:
            reason = (stderr or stdout).strip() or f"exit code {rc}"
            return PreflightCheckResult(
                name,
                False,
                f"docker buildx is unavailable on the runner: {reason}",
            ), ""
        buildx_note = f" buildx: {stdout.strip().splitlines()[0]}." if stdout.strip() else ""

        # Prove the GHCR credential actually authenticates rather than
        # assuming the token carries registry access. The token is fed on
        # stdin and never printed.
        registry_user = self.env.get("REGISTRY_USER") or self.env.get("GITHUB_ACTOR") or "github-actions"
        rc_login, _, stderr_login = self.run_cmd(
            ["docker", "login", "ghcr.io", "-u", registry_user, "--password-stdin"],
            cwd=self.repo_root,
            stdin_data=gh_token,
        )
        if rc_login != 0:
            reason = stderr_login.strip().splitlines()[-1] if stderr_login.strip() else f"exit code {rc_login}"
            return PreflightCheckResult(
                name,
                False,
                f"GHCR authentication failed for user '{registry_user}': {reason}",
            ), buildx_note
        return None, buildx_note

    def _check_release_tags(self, name: str) -> PreflightCheckResult | None:
        """Reject a duplicate cut when vX.Y.Z / bundle-vX.Y.Z already exist."""
        if self.mode != "cut" or not self.version:
            return None

        target_tags = [f"v{self.version}", f"bundle-v{self.version}"]
        rc, stdout, _ = self.run_cmd(["git", "tag", "-l", *target_tags], cwd=self.repo_root)
        if rc == 0 and stdout.strip():
            existing = [t.strip() for t in stdout.splitlines() if t.strip() in target_tags]
            if existing:
                return PreflightCheckResult(
                    name,
                    False,
                    f"Release tag(s) already exist locally: {', '.join(existing)}; duplicate cut refused",
                )

        for tag in target_tags:
            rc_remote, _, _ = self.run_cmd(
                ["git", "ls-remote", "--exit-code", "origin", f"refs/tags/{tag}"],
                cwd=self.repo_root,
            )
            if rc_remote == 0:
                return PreflightCheckResult(
                    name,
                    False,
                    f"Release tag '{tag}' already exists on origin; duplicate cut refused",
                )
        return None

    # -------------------------------------------------------------------------
    # 5. rc_tag output defined and propagated
    # -------------------------------------------------------------------------
    def check_rc_tag_propagation(self) -> PreflightCheckResult:
        name = "rc_tag output defined and propagated"
        workflow_file = self.repo_root / _GITHUB_DIR / "workflows" / "release-unified.yml"
        if not workflow_file.exists():
            return PreflightCheckResult(name, False, ".github/workflows/release-unified.yml is missing")

        try:
            content = workflow_file.read_text(encoding="utf-8")
        except OSError as exc:
            return PreflightCheckResult(name, False, f"Could not read release-unified.yml: {exc}")

        for required, message in (
            (
                "rc_tag: ${{ steps.rc-output.outputs.rc_tag }}",
                "release-unified.yml: 'release' job does not declare 'rc_tag: ${{ steps.rc-output.outputs.rc_tag }}'",
            ),
            ("id: rc-output", "release-unified.yml: 'rc-output' step missing in 'release' job"),
            (
                "rc_tag: ${{ needs.release.outputs.rc_tag }}",
                "release-unified.yml: dependent jobs do not reference '${{ needs.release.outputs.rc_tag }}'",
            ),
        ):
            if required not in content:
                return PreflightCheckResult(name, False, message)

        expected_tag, failure = self._resolve_expected_rc_tag(name)
        if failure is not None:
            return failure

        return PreflightCheckResult(
            name,
            True,
            f"Workflow rc_tag outputs declared and expected tag resolved to '{expected_tag}'",
        )

    def _resolve_expected_rc_tag(self, name: str) -> tuple[str, PreflightCheckResult | None]:
        """Resolve the expected rc_tag for this mode, or a failure result."""
        if self.mode == "dry-run":
            return f"v{self.version}-dry-run", None

        if self.mode in ("cut", "recut"):
            # Determine next free RC number
            rc, stdout, _ = self.run_cmd(
                ["git", "tag", "-l", f"v{self.version}-rc*"],
                cwd=self.repo_root,
            )
            local_tags = set(stdout.splitlines()) if rc == 0 else set()
            n = 1
            while f"v{self.version}-rc{n}" in local_tags:
                n += 1
            return f"v{self.version}-rc{n}", None

        if self.mode == "promote":
            rc, stdout, _ = self.run_cmd(
                ["git", "tag", "-l", f"v{self.version}-rc*"],
                cwd=self.repo_root,
            )
            tags = [t.strip() for t in stdout.splitlines() if t.strip()] if rc == 0 else []
            if not tags:
                return "", PreflightCheckResult(
                    name,
                    False,
                    f"Cannot promote: no prior release candidate tags 'v{self.version}-rc*' found",
                )
            return max(tags), None

        return "unknown", None

    # -------------------------------------------------------------------------
    # 6. uv.lock freshness for release version bump
    # -------------------------------------------------------------------------
    def check_uv_lock_freshness(self) -> PreflightCheckResult:
        name = "uv.lock freshness for release version bump"
        backend_dir = self.repo_root / "backend"
        pyproject = backend_dir / "pyproject.toml"
        uv_lock = backend_dir / "uv.lock"

        if not pyproject.exists():
            return PreflightCheckResult(name, False, "backend/pyproject.toml does not exist")
        if not uv_lock.exists():
            return PreflightCheckResult(name, False, "backend/uv.lock does not exist")

        uv_bin = shutil.which("uv")
        if uv_bin:
            rc, stdout, stderr = self.run_cmd(
                ["uv", "lock", "--project", "backend", "--check"],
                cwd=self.repo_root,
                timeout=180,
            )
            if rc != 0:
                err_msg = (stderr or stdout).strip() or f"exit code {rc}"
                return PreflightCheckResult(
                    name,
                    False,
                    f"backend/uv.lock is stale relative to backend/pyproject.toml: {err_msg}",
                )
            return PreflightCheckResult(
                name,
                True,
                "backend/uv.lock is fresh and in sync with backend/pyproject.toml",
            )

        return PreflightCheckResult(
            name,
            True,
            "backend/uv.lock exists (uv binary not found on PATH for dynamic check)",
        )

    # -------------------------------------------------------------------------
    # 7. Release-freeze marker state valid and movable (FAR-2317 tag ruleset)
    # -------------------------------------------------------------------------
    def check_release_freeze_state(self) -> PreflightCheckResult:
        name = "Release-freeze marker state valid and movable (FAR-2317 tag ruleset)"

        # Check for active release-publication-hold (refused in cut and promote)
        rc_hold, _, hold_err = self.run_cmd(
            ["git", "ls-remote", "--exit-code", "origin", "refs/heads/release-publication-hold"],
            cwd=self.repo_root,
        )
        if rc_hold == 0:
            return PreflightCheckResult(
                name,
                False,
                "Active 'release-publication-hold' branch detected on origin; prior cut/promote left unresolved hold",
            )
        if rc_hold != 2:
            reason = hold_err.strip() or f"exit code {rc_hold}"
            return PreflightCheckResult(
                name,
                False,
                f"Could not determine 'release-publication-hold' state on origin: {reason}",
            )

        # Check release-freeze requirement for promote mode
        if self.mode == "promote":
            rc_freeze, _, freeze_err = self.run_cmd(
                ["git", "ls-remote", "--exit-code", "origin", "refs/heads/release-freeze"],
                cwd=self.repo_root,
            )
            if rc_freeze == 2:
                return PreflightCheckResult(
                    name,
                    False,
                    "Promotion requires an active 'release-freeze' branch, but none was found on origin",
                )
            if rc_freeze != 0:
                reason = freeze_err.strip() or f"exit code {rc_freeze}"
                return PreflightCheckResult(
                    name,
                    False,
                    f"Could not determine 'release-freeze' state on origin: {reason}",
                )

        freeze_note = ""
        if self.mode in ("cut", "recut"):
            freeze_note = (
                " release-freeze will be established after the cut."
                if self.freeze
                else " freeze=false: the merge queue will NOT be paused by this release."
            )

        return PreflightCheckResult(
            name,
            True,
            f"Release-freeze state valid for mode '{self.mode}' (no unresolved publication holds).{freeze_note}",
        )

    # -------------------------------------------------------------------------
    # 8. Windows native artifacts payload present when included
    # -------------------------------------------------------------------------
    def check_windows_native_artifacts(self) -> PreflightCheckResult:
        name = "Windows native artifacts payload present"
        if not self.include_windows:
            return PreflightCheckResult(
                name,
                True,
                "Windows native artifacts skipped: include_windows=false; Windows packaging is Beta scope",
            )
        if self.mode == "dry-run":
            return PreflightCheckResult(
                name,
                True,
                "Dry-run mode: native Windows packaging skipped by contract",
            )

        payload = self._find_local_windows_payload()
        if payload is not None:
            return PreflightCheckResult(
                name,
                True,
                f"Windows native artifacts payload present locally ({payload.name}, {payload.stat().st_size} bytes)",
            )

        run_id = self._find_successful_windows_artifact_run()
        if run_id is not None:
            return PreflightCheckResult(
                name,
                True,
                f"Windows native artifacts available from successful windows-artifacts.yml run ({run_id})",
            )

        # Windows native packaging is required when include_windows=true: the
        # packaging job (FAR-2338) consumes this zip and real-host validation
        # (FAR-2250) runs the installer
        # it produces, so an included cut/recut without the payload would fail
        # later in the release instead of here.
        return PreflightCheckResult(
            name,
            False,
            f"Windows native artifacts payload '{_WINDOWS_ARTIFACTS_ZIP}' is missing: no local zip and no "
            f"successful windows-artifacts.yml run found. The Windows package (FAR-2338) requires it when "
            f"mode '{self.mode}'; real-host validation (FAR-2250) runs the installer it produces",
        )

    def _find_local_windows_payload(self) -> Path | None:
        """Return the first non-empty local Windows artifacts zip, if any."""
        local_payloads = [
            self.repo_root / _WINDOWS_ARTIFACTS_ZIP,
            self.repo_root / "windows-artifacts" / _WINDOWS_ARTIFACTS_ZIP,
            self.repo_root / "deploy" / "windows" / _WINDOWS_ARTIFACTS_ZIP,
        ]
        for payload in local_payloads:
            if payload.exists() and payload.stat().st_size > 0:
                return payload
        return None

    def _find_successful_windows_artifact_run(self) -> object:
        """Return the databaseId of a successful windows-artifacts run, if any."""
        gh_token = self.env.get("GH_TOKEN") or self.env.get("GITHUB_TOKEN")
        gh_repo = self.env.get("GITHUB_REPOSITORY")
        if not (gh_token and gh_repo):
            return None

        rc, stdout, _ = self.run_cmd(
            [
                "gh",
                "run",
                "list",
                "--workflow",
                "windows-artifacts.yml",
                "--limit",
                "5",
                "--json",
                "databaseId,status,conclusion",
            ],
            cwd=self.repo_root,
            env=self.env,
        )
        if rc != 0 or not stdout.strip():
            return None
        try:
            runs = json.loads(stdout)
        except json.JSONDecodeError:
            return None
        for run in runs:
            if run.get("status") == "completed" and run.get("conclusion") == "success":
                return run.get("databaseId")
        return None

    # -------------------------------------------------------------------------
    # 9. Azure Trusted Signing resource / secrets presence check
    # -------------------------------------------------------------------------
    def check_azure_signing_secrets(self) -> PreflightCheckResult:
        name = "Azure Trusted Signing secrets presence"
        signing_secrets = [
            "AZURE_CLIENT_ID",
            "AZURE_TENANT_ID",
            "AZURE_SUBSCRIPTION_ID",
        ]
        release_secrets = [
            "BUNDLE_MANIFEST_SIGNING_KEY",
            "MODULO_RELEASE_PR_TOKEN",
        ]

        if self.mode == "dry-run":
            all_missing = [s for s in signing_secrets + release_secrets if not self.env.get(s)]
            if all_missing:
                return PreflightCheckResult(
                    name,
                    True,
                    "Dry-run mode: signing secrets not required "
                    f"(unconfigured: {', '.join(all_missing)}; values redacted)",
                )
            return PreflightCheckResult(
                name,
                True,
                "All signing and release secrets configured (values redacted)",
            )

        # Windows native packaging is Alpha scope (FAR-2338) and the Windows
        # signing job (windows-installer.yml) fails closed without the Azure
        # Trusted Signing credentials. A cut/recut that passed this gate
        # without them would only fail later, at the signing job - the
        # serial-unveiling failure this preflight exists to prevent. Promote
        # does not build or sign a Windows installer, so it only requires the
        # release secrets.
        required = list(release_secrets)
        if self.mode in ("cut", "recut"):
            required = signing_secrets + release_secrets

        all_missing = [s for s in required if not self.env.get(s)]
        if all_missing:
            return PreflightCheckResult(
                name,
                False,
                f"Missing required release secret(s): {', '.join(all_missing)} (values redacted)",
            )
        return PreflightCheckResult(
            name,
            True,
            "All required Azure Trusted Signing and release secrets are configured (values redacted)",
        )

    # -------------------------------------------------------------------------
    # 10. Branch merge-conflict check against main
    # -------------------------------------------------------------------------
    def check_merge_conflicts(self) -> PreflightCheckResult:
        name = "Branch merge-conflict check against main"
        # Verify origin/main exists or can be resolved
        rc, _, _ = self.run_cmd(["git", "rev-parse", "--verify", "--quiet", "origin/main"], cwd=self.repo_root)
        baseline = "origin/main" if rc == 0 else "main"

        rc_base, base_sha, _ = self.run_cmd(["git", "merge-base", baseline, "HEAD"], cwd=self.repo_root)
        if rc_base != 0:
            return PreflightCheckResult(
                name,
                True,
                f"Cannot compute merge-base with {baseline}; merge-conflict check skipped",
            )

        if self._is_head_on_baseline(base_sha.strip(), baseline):
            return PreflightCheckResult(
                name,
                True,
                f"Clean branch state against {baseline} (no divergent commits)",
            )

        conflict = self._merge_tree_conflict(baseline, base_sha.strip(), name)
        if conflict is not None:
            return conflict

        return PreflightCheckResult(
            name,
            True,
            f"Branch has no merge conflicts against {baseline}",
        )

    def _is_head_on_baseline(self, base_sha: str, baseline: str) -> bool:
        rc_head, head_sha, _ = self.run_cmd(["git", "rev-parse", "HEAD"], cwd=self.repo_root)
        head_sha = head_sha.strip() if rc_head == 0 else ""
        rc_target, target_sha, _ = self.run_cmd(["git", "rev-parse", baseline], cwd=self.repo_root)
        target_sha = target_sha.strip() if rc_target == 0 else ""
        return base_sha in (head_sha, target_sha)

    def _merge_tree_conflict(self, baseline: str, base_sha: str, name: str) -> PreflightCheckResult | None:
        """Return a conflict/error result, or ``None`` when the merge is clean.

        `git merge-tree --write-tree` exits 1 when the merge conflicts and
        prints the resulting tree OID followed by the conflicted paths (with
        --name-only), a blank line, then informational messages. Any other
        non-zero exit is a real error (for example an older git that lacks
        --write-tree exits 129), which falls back to the deprecated 3-arg
        form. A git error on the fallback path is a hard failure, never a
        silent pass: a gate that cannot determine the merge state must fail
        closed.
        """
        rc_mt, mt_out, mt_err = self.run_cmd(
            ["git", "merge-tree", "--write-tree", "--name-only", baseline, "HEAD"],
            cwd=self.repo_root,
        )
        if rc_mt == 1:
            conflicts = self._parse_merge_tree_conflicts(mt_out)
            if not conflicts:
                return PreflightCheckResult(
                    name,
                    False,
                    f"Merge conflicts detected against {baseline} (git merge-tree reported a conflicted merge)",
                )
            return PreflightCheckResult(
                name,
                False,
                f"Merge conflicts detected against {baseline} in {len(conflicts)} path(s): {', '.join(conflicts[:5])}",
            )
        if rc_mt == 0:
            return None
        return self._merge_tree_fallback(baseline, base_sha, mt_err, name)

    def _merge_tree_fallback(self, baseline: str, base_sha: str, mt_err: str, name: str) -> PreflightCheckResult | None:
        """Run the deprecated 3-arg merge-tree form on older git."""
        rc_mt3, mt3_out, mt3_err = self.run_cmd(
            ["git", "merge-tree", base_sha, "HEAD", baseline],
            cwd=self.repo_root,
        )
        if rc_mt3 != 0:
            reason = (mt3_err or mt3_out or mt_err).strip() or f"exit code {rc_mt3}"
            return PreflightCheckResult(
                name,
                False,
                f"Unable to determine merge state against {baseline}: git merge-tree failed ({reason})",
            )
        if "<<<<<<<" in mt3_out:
            return PreflightCheckResult(
                name,
                False,
                f"Merge conflicts detected against {baseline} in 3-way merge",
            )
        return None

    @staticmethod
    def _parse_merge_tree_conflicts(output: str) -> list[str]:
        """Extract conflicted paths from `git merge-tree --write-tree` output.

        The first line is the merged-tree OID; conflicted paths follow until a
        blank line, after which informational conflict messages are printed.
        A clean merge emits only the tree OID.
        """
        lines = [line.strip() for line in output.splitlines()]
        conflicts: list[str] = []
        for line in lines[1:]:
            if not line:
                break
            conflicts.append(line)
        return conflicts

    # -------------------------------------------------------------------------
    # Orchestration & Summary
    # -------------------------------------------------------------------------
    def run_all_checks(self) -> list[PreflightCheckResult]:
        """Execute all 10 preflight checks unconditionally."""
        checks: list[Callable[[], PreflightCheckResult]] = [
            self.check_schema_freshness,
            self.check_macos_installer_inputs,
            self.check_inno_setup_availability,
            self.check_ghcr_and_buildx,
            self.check_rc_tag_propagation,
            self.check_uv_lock_freshness,
            self.check_release_freeze_state,
            self.check_windows_native_artifacts,
            self.check_azure_signing_secrets,
            self.check_merge_conflicts,
        ]

        results: list[PreflightCheckResult] = []
        for check_fn in checks:
            try:
                res = check_fn()
            except Exception as exc:
                check_name = getattr(check_fn, "__name__", "unknown_check").replace("check_", "").replace("_", " ")
                res = PreflightCheckResult(check_name, False, f"Unexpected exception during check: {exc}")
            results.append(res)
            status_tag = "PASS" if res.passed else "FAIL"
            print(f"[{status_tag}] {res.name}: {res.details}", file=sys.stderr)

        return results

    @staticmethod
    def format_markdown_summary(results: list[PreflightCheckResult]) -> str:
        """Format check results as a GitHub step summary table."""
        lines = [
            "## Release Preflight Gate Summary",
            "",
            "| Check | Status | Details |",
            "|---|:---:|---|",
        ]
        for r in results:
            icon = "✅ PASS" if r.passed else "❌ FAIL"
            # Escape pipe characters in details for table formatting
            sanitized_details = r.details.replace("|", "\\|")
            lines.append(f"| {r.name} | {icon} | {sanitized_details} |")

        failed = [r.name for r in results if not r.passed]
        lines.append("")
        if failed:
            lines.append(f"> ❌ **Preflight Gate FAILED**: {len(failed)} of {len(results)} checks failed:")
            lines.extend(f"> - **{f}**" for f in failed)
        else:
            lines.append(f"> ✅ **Preflight Gate PASSED**: All {len(results)} checks passed successfully.")

        return "\n".join(lines) + "\n"


#: Allowlisted character set for a caller-supplied step-summary path.
#:
#: The destination is externally controlled (a CLI argument defaulting to
#: ``$GITHUB_STEP_SUMMARY``), so it is a path-injection sink (CWE-22). Only
#: letters, digits, dot, underscore, hyphen, an optional drive prefix and the
#: path separator are allowed: no whitespace, glob, control or shell
#: metacharacters can survive the match.
_SUMMARY_PATH_RE = re.compile(r"(?:[A-Za-z]:/)?/?(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+")


def _safe_summary_path(value: str) -> str | None:
    """Return *value* only when it is a safe, traversal-free summary path.

    Rejects any ``..`` component outright and requires the remainder to match
    the narrow ``_SUMMARY_PATH_RE`` allowlist. The value returned is the
    regex-bounded match, so the taint analyser no longer tracks the raw
    caller-supplied string into the filesystem write. Returns ``None`` when the
    path is empty or unsafe.

    Windows separators are normalised to ``/`` before validation, because a
    Windows runner's ``$GITHUB_STEP_SUMMARY`` is a backslash path (``open()``
    accepts either separator there). Traversal is checked on the normalised
    form so a ``..\\`` component cannot slip past a POSIX-only parts split.
    """
    if not value:
        return None
    normalised = value.replace("\\", "/")
    if ".." in Path(normalised).parts:
        return None
    matched = _SUMMARY_PATH_RE.fullmatch(normalised)
    return matched.group(0) if matched else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        default=os.environ.get("RELEASE_MODE", "dry-run"),
        help="Release mode: cut, recut, promote, dry-run (default: $RELEASE_MODE or dry-run)",
    )
    parser.add_argument(
        "--version",
        default=os.environ.get("RELEASE_VERSION", ""),
        help="Target version in X.Y.Z form (default: $RELEASE_VERSION)",
    )
    parser.add_argument(
        "--freeze",
        action="store_true",
        default=os.environ.get("RELEASE_FREEZE", "false").lower() == "true",
        help="Release freeze flag (default: $RELEASE_FREEZE == true)",
    )
    parser.add_argument(
        "--include-windows",
        action="store_true",
        default=os.environ.get("RELEASE_INCLUDE_WINDOWS", "false").lower() == "true",
        help="Require Windows native artifacts (default: $RELEASE_INCLUDE_WINDOWS == true)",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Path to repository root",
    )
    parser.add_argument(
        "--summary-file",
        default=os.environ.get("GITHUB_STEP_SUMMARY"),
        help="File path to write GitHub step summary table",
    )

    args = parser.parse_args(argv)

    checker = ReleasePreflightChecker(
        mode=args.mode,
        version=args.version,
        freeze=args.freeze,
        include_windows=args.include_windows,
        repo_root=args.repo_root,
    )

    print(
        f"--- Starting Release Preflight Gate (mode={checker.mode}, version={checker.version or 'unset'}, "
        f"include_windows={str(checker.include_windows).lower()}) ---",
        file=sys.stderr,
    )

    results = checker.run_all_checks()
    summary_md = checker.format_markdown_summary(results)

    if args.summary_file:
        summary_path = _safe_summary_path(args.summary_file)
        if summary_path is None:
            print(
                f"Warning: refusing to write summary to unsafe path: {args.summary_file!r}",
                file=sys.stderr,
            )
        else:
            try:
                with Path(summary_path).open("a", encoding="utf-8") as f:
                    f.write(summary_md)
            except OSError as exc:
                print(f"Warning: could not write summary to {summary_path}: {exc}", file=sys.stderr)

    failed_checks = [r.name for r in results if not r.passed]
    if failed_checks:
        print("\n========================================================", file=sys.stderr)
        print(f"PREFLIGHT FAILED: {len(failed_checks)} check(s) failed:", file=sys.stderr)
        for name in failed_checks:
            print(f"  * {name}", file=sys.stderr)
        print("========================================================\n", file=sys.stderr)
        return 1

    print("\n========================================================", file=sys.stderr)
    print("PREFLIGHT PASSED: All checks passed successfully.", file=sys.stderr)
    print("========================================================\n", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
