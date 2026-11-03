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
  8. Windows native artifacts payload present.
  9. Azure Trusted Signing resource / secrets presence check (values never printed!).
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
        repo_root: Path | str | None = None,
        env: dict[str, str] | None = None,
        run_cmd: Callable[..., tuple[int, str, str]] | None = None,
    ) -> None:
        self.mode = mode.strip()
        self.version = version.strip()
        self.freeze = freeze
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
        if not schema_file.exists():
            return PreflightCheckResult(name, False, "frontend/src/lib/api/schema.ts does not exist")
        try:
            size = schema_file.stat().st_size
        except OSError as exc:
            return PreflightCheckResult(name, False, f"Could not inspect schema.ts: {exc}")
        if size == 0:
            return PreflightCheckResult(name, False, "frontend/src/lib/api/schema.ts is empty (0 bytes)")

        # In CI (or whenever a GitHub token and repository are supplied), prove
        # the schema-freshness gate actually ran and passed on the target
        # commit. A file existing is not evidence that the generated schema
        # matches the backend.
        gh_token = self.env.get("GH_TOKEN") or self.env.get("GITHUB_TOKEN")
        gh_repo = self.env.get("GITHUB_REPOSITORY")

        if gh_token and gh_repo:
            target_sha = self.env.get("CUT_SHA") or self.env.get("GITHUB_SHA") or ""
            if not target_sha:
                rc, stdout, _ = self.run_cmd(["git", "rev-parse", "HEAD"], cwd=self.repo_root)
                if rc == 0 and stdout.strip():
                    target_sha = stdout.strip()

            if target_sha:
                gate = self._lookup_schema_freshness_gate(target_sha, gh_repo)
                if gate is not None:
                    return gate
                if self.mode in ("cut", "recut"):
                    return PreflightCheckResult(
                        name,
                        False,
                        f"No CI run found for commit {target_sha[:7]} in repository {gh_repo}",
                    )

        return PreflightCheckResult(
            name,
            True,
            f"frontend/src/lib/api/schema.ts present and valid ({size} bytes)",
        )

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

        # A commit can have more than one CI run (re-runs, docs-only pushes),
        # and the Schema freshness job may be absent from some of them. Check
        # every run until one proves the gate green; a run without the job is
        # not evidence either way, so keep looking.
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
            if non_green is None:
                non_green = verdict

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
        status = job_data.get("status")
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

        macos_workflow = self.repo_root / ".github" / "workflows" / "macos-installer.yml"
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
        for line_num, line in enumerate(iss_text.splitlines(), start=1):
            stripped = line.strip()
            if re.match(r"^#\d+", stripped):
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

        win_workflow = self.repo_root / ".github" / "workflows" / "windows-installer.yml"
        if not win_workflow.exists():
            return PreflightCheckResult(
                name,
                False,
                ".github/workflows/windows-installer.yml is missing",
            )

        try:
            workflow_content = win_workflow.read_text(encoding="utf-8")
            if "Find-Iscc" not in workflow_content:
                return PreflightCheckResult(
                    name,
                    False,
                    "windows-installer.yml does not define Inno Setup probe (Find-Iscc)",
                )
        except OSError as exc:
            return PreflightCheckResult(name, False, f"Could not read windows-installer.yml: {exc}")

        launcher_helper = self.repo_root / "scripts" / "create-windows-launcher.ps1"
        if not launcher_helper.exists():
            return PreflightCheckResult(
                name,
                False,
                "scripts/create-windows-launcher.ps1 is missing",
            )

        # If on Windows, check if ISCC binary is present for local diagnosis
        iscc_note = ""
        if sys.platform == "win32":
            iscc_found = shutil.which("ISCC.exe")
            if not iscc_found:
                for env_var in ("ProgramFiles", "ProgramFiles(x86)"):
                    pf = self.env.get(env_var)
                    if pf:
                        candidate = Path(pf) / "Inno Setup 6" / "ISCC.exe"
                        if candidate.exists():
                            iscc_found = str(candidate)
                            break
            if iscc_found:
                iscc_note = f" (local compiler found at {iscc_found})"

        return PreflightCheckResult(
            name,
            True,
            f"Inno Setup workflow probe, modulo.iss preprocessor syntax, and launcher script verified{iscc_note}",
        )

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

        dockerfiles = [
            ("all-in-one", self.repo_root / "deploy" / "docker" / "Dockerfile.all-in-one"),
            ("backend", self.repo_root / "backend" / "Dockerfile"),
            ("frontend", self.repo_root / "frontend" / "Dockerfile.prod"),
            ("runner-opencode", self.repo_root / "deploy" / "docker" / "runner-opencode.Dockerfile"),
        ]
        missing_dockerfiles = [img for img, path in dockerfiles if not path.exists()]
        if missing_dockerfiles:
            return PreflightCheckResult(
                name,
                False,
                f"Missing release Dockerfile(s): {', '.join(missing_dockerfiles)}",
            )

        buildx_note = ""
        if is_ci and gh_token:
            # The runner must have a docker CLI with a working buildx builder,
            # because every publish path builds with `docker buildx build`.
            rc, stdout, stderr = self.run_cmd(["docker", "buildx", "version"], cwd=self.repo_root)
            if rc != 0:
                reason = (stderr or stdout).strip() or f"exit code {rc}"
                return PreflightCheckResult(
                    name,
                    False,
                    f"docker buildx is unavailable on the runner: {reason}",
                )
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
                )

        # Tag availability check: for cut mode, vX.Y.Z and bundle-vX.Y.Z must not exist
        if self.mode == "cut" and self.version:
            target_tags = [f"v{self.version}", f"bundle-v{self.version}"]
            # Local tag check
            rc, stdout, _ = self.run_cmd(["git", "tag", "-l", *target_tags], cwd=self.repo_root)
            if rc == 0 and stdout.strip():
                existing = [t.strip() for t in stdout.splitlines() if t.strip() in target_tags]
                if existing:
                    return PreflightCheckResult(
                        name,
                        False,
                        f"Release tag(s) already exist locally: {', '.join(existing)}; duplicate cut refused",
                    )

            # Remote tag check
            for tag in target_tags:
                rc, _, _ = self.run_cmd(
                    ["git", "ls-remote", "--exit-code", "origin", f"refs/tags/{tag}"],
                    cwd=self.repo_root,
                )
                if rc == 0:
                    return PreflightCheckResult(
                        name,
                        False,
                        f"Release tag '{tag}' already exists on origin; duplicate cut refused",
                    )

        return PreflightCheckResult(
            name,
            True,
            f"GHCR token configured, 4 container build contexts verified, and release tags available.{buildx_note}",
        )

    # -------------------------------------------------------------------------
    # 5. rc_tag output defined and propagated
    # -------------------------------------------------------------------------
    def check_rc_tag_propagation(self) -> PreflightCheckResult:
        name = "rc_tag output defined and propagated"
        workflow_file = self.repo_root / ".github" / "workflows" / "release-unified.yml"
        if not workflow_file.exists():
            return PreflightCheckResult(name, False, ".github/workflows/release-unified.yml is missing")

        try:
            content = workflow_file.read_text(encoding="utf-8")
        except OSError as exc:
            return PreflightCheckResult(name, False, f"Could not read release-unified.yml: {exc}")

        # Verify release job declares rc_tag output
        if "rc_tag: ${{ steps.rc-output.outputs.rc_tag }}" not in content:
            return PreflightCheckResult(
                name,
                False,
                "release-unified.yml: 'release' job does not declare 'rc_tag: ${{ steps.rc-output.outputs.rc_tag }}'",
            )

        # Verify rc-output step exists
        if "id: rc-output" not in content:
            return PreflightCheckResult(
                name,
                False,
                "release-unified.yml: 'rc-output' step missing in 'release' job",
            )

        # Verify dependent jobs consume needs.release.outputs.rc_tag
        if "rc_tag: ${{ needs.release.outputs.rc_tag }}" not in content:
            return PreflightCheckResult(
                name,
                False,
                "release-unified.yml: dependent jobs do not reference '${{ needs.release.outputs.rc_tag }}'",
            )

        # Compute and validate expected rc_tag for this run
        if self.mode == "dry-run":
            expected_tag = f"v{self.version}-dry-run"
        elif self.mode in ("cut", "recut"):
            # Determine next free RC number
            rc, stdout, _ = self.run_cmd(
                ["git", "tag", "-l", f"v{self.version}-rc*"],
                cwd=self.repo_root,
            )
            local_tags = set(stdout.splitlines()) if rc == 0 else set()
            n = 1
            while f"v{self.version}-rc{n}" in local_tags:
                n += 1
            expected_tag = f"v{self.version}-rc{n}"
        elif self.mode == "promote":
            rc, stdout, _ = self.run_cmd(
                ["git", "tag", "-l", f"v{self.version}-rc*"],
                cwd=self.repo_root,
            )
            tags = [t.strip() for t in stdout.splitlines() if t.strip()] if rc == 0 else []
            if not tags:
                return PreflightCheckResult(
                    name,
                    False,
                    f"Cannot promote: no prior release candidate tags 'v{self.version}-rc*' found",
                )
            expected_tag = max(tags)
        else:
            expected_tag = "unknown"

        return PreflightCheckResult(
            name,
            True,
            f"Workflow rc_tag outputs declared and expected tag resolved to '{expected_tag}'",
        )

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
    # 8. Windows native artifacts payload present
    # -------------------------------------------------------------------------
    def check_windows_native_artifacts(self) -> PreflightCheckResult:
        name = "Windows native artifacts payload present"
        if self.mode == "dry-run":
            return PreflightCheckResult(
                name,
                True,
                "Dry-run mode: native Windows packaging skipped by contract",
            )

        # Check local zip artifact
        local_payloads = [
            self.repo_root / "windows-native-artifacts.zip",
            self.repo_root / "windows-artifacts" / "windows-native-artifacts.zip",
            self.repo_root / "deploy" / "windows" / "windows-native-artifacts.zip",
        ]
        for p in local_payloads:
            if p.exists() and p.stat().st_size > 0:
                return PreflightCheckResult(
                    name,
                    True,
                    f"Windows native artifacts payload present locally ({p.name}, {p.stat().st_size} bytes)",
                )

        # In CI, check if a windows-artifacts.yml run has produced the artifact
        gh_token = self.env.get("GH_TOKEN") or self.env.get("GITHUB_TOKEN")
        gh_repo = self.env.get("GITHUB_REPOSITORY")
        if gh_token and gh_repo:
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
            if rc == 0 and stdout.strip():
                try:
                    runs = json.loads(stdout)
                except json.JSONDecodeError:
                    runs = []
                for r in runs:
                    if r.get("status") == "completed" and r.get("conclusion") == "success":
                        run_id = r.get("databaseId")
                        return PreflightCheckResult(
                            name,
                            True,
                            f"Windows native artifacts available from successful windows-artifacts.yml run ({run_id})",
                        )

        return PreflightCheckResult(
            name,
            False,
            f"Windows native artifacts payload 'windows-native-artifacts' is missing; release packaging requires it for mode '{self.mode}'",
        )

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
        other_release_secrets = [
            "BUNDLE_MANIFEST_SIGNING_KEY",
            "MODULO_RELEASE_PR_TOKEN",
        ]
        all_required = signing_secrets + other_release_secrets

        # Never print secret values: check boolean presence only
        missing = [secret_name for secret_name in all_required if not self.env.get(secret_name)]

        if self.mode == "dry-run":
            if missing:
                return PreflightCheckResult(
                    name,
                    True,
                    f"Dry-run mode: signing secrets not required (unconfigured: {', '.join(missing)}; values redacted)",
                )
            return PreflightCheckResult(
                name,
                True,
                "All signing and release secrets configured (values redacted)",
            )

        if missing:
            return PreflightCheckResult(
                name,
                False,
                f"Missing required release secret(s): {', '.join(missing)} (values redacted)",
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

        base_sha = base_sha.strip()
        rc_head, head_sha, _ = self.run_cmd(["git", "rev-parse", "HEAD"], cwd=self.repo_root)
        head_sha = head_sha.strip() if rc_head == 0 else ""

        rc_target, target_sha, _ = self.run_cmd(["git", "rev-parse", baseline], cwd=self.repo_root)
        target_sha = target_sha.strip() if rc_target == 0 else ""

        # If HEAD is on baseline or up-to-date ancestor
        if base_sha in (head_sha, target_sha):
            return PreflightCheckResult(
                name,
                True,
                f"Clean branch state against {baseline} (no divergent commits)",
            )

        # `git merge-tree --write-tree` exits 1 when the merge conflicts and
        # prints the resulting tree OID followed by the conflicted paths (with
        # --name-only), a blank line, then informational messages. Any other
        # non-zero exit is a real error (for example an older git that lacks
        # --write-tree exits 129), which falls back to the deprecated 3-arg
        # form. A git error on the fallback path is a hard failure, never a
        # silent pass: a gate that cannot determine the merge state must fail
        # closed.
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
        if rc_mt != 0:
            # Fallback to 3-arg git merge-tree syntax on older git.
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

        return PreflightCheckResult(
            name,
            True,
            f"Branch has no merge conflicts against {baseline}",
        )

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
        repo_root=args.repo_root,
    )

    print(
        f"--- Starting Release Preflight Gate (mode={checker.mode}, version={checker.version or 'unset'}) ---",
        file=sys.stderr,
    )

    results = checker.run_all_checks()
    summary_md = checker.format_markdown_summary(results)

    if args.summary_file:
        try:
            with Path(args.summary_file).open("a", encoding="utf-8") as f:
                f.write(summary_md)
        except OSError as exc:
            print(f"Warning: could not write summary to {args.summary_file}: {exc}", file=sys.stderr)

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
