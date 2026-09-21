#!/usr/bin/env python3
"""Unit tests for scripts/fast_lane_classify.py.

Run from the repo root:
    uv run --project backend pytest tests/unit/scripts/test_fast_lane_classify.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the scripts directory is importable.
_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from fast_lane_classify import classify_path, classify_paths  # noqa: E402

# ---------------------------------------------------------------------------
# classify_path
# ---------------------------------------------------------------------------


class TestClassifyPath:
    """Each path must be class-A or class-B per the spec."""

    # --- Class-A paths ---
    @pytest.mark.parametrize(
        "path",
        [
            "backend/tests/unit/test_foo.py",
            "backend/tests/integration/test_bar.py",
            "frontend/tests/unit/baz.spec.ts",
            "tests/architecture/test_suite_quality.py",
            "docs/architecture.md",
            "README.md",
            ".github/CODEOWNERS",
            ".github/ISSUE_TEMPLATE/bug.md",
            ".github/pull_request_template.md",
        ],
    )
    def test_class_a_paths(self, path: str) -> None:
        assert classify_path(path) == "A", f"{path} should be class-A"

    # --- Class-B paths ---
    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/ci.yml",
            ".github/workflows/deploy.yaml",
            ".github/scripts/release-freeze-check.py",
            "scripts/fast_lane_classify.py",
            "pyproject.toml",
            "backend/pyproject.toml",
            "frontend/package.json",
            ".semgrep/rls_set_local.yml",
            ".pre-commit-config.yaml",
            "backend/src/modulo/api/routes/pipelines.py",
            "frontend/src/views/PipelinesView.vue",
            "fly.toml",
            "deploy/fly/entrypoint.sh",
            "backend/src/modulo/db/migrations/versions/0001_initial.py",
            "backend/src/modulo/core/pipeline_engine/graph.py",
        ],
    )
    def test_class_b_paths(self, path: str) -> None:
        assert classify_path(path) == "B", f"{path} should be class-B"


# ---------------------------------------------------------------------------
# classify_paths
# ---------------------------------------------------------------------------


class TestClassifyPaths:
    """A PR is class-A only if ALL its paths are class-A."""

    def test_all_a(self) -> None:
        paths = [
            "backend/tests/unit/test_foo.py",
            "docs/architecture.md",
        ]
        assert classify_paths(paths) == "A"

    def test_one_b_makes_all_b(self) -> None:
        paths = [
            "backend/tests/unit/test_foo.py",
            "backend/src/modulo/api/routes/pipelines.py",  # class-B
        ]
        assert classify_paths(paths) == "B"

    def test_empty_is_b(self) -> None:
        # Empty PR has no test changes — treat as class-B (not fast-lane eligible).
        assert classify_paths([]) == "B"

    def test_github_non_workflow_is_a(self) -> None:
        paths = [".github/CODEOWNERS"]
        assert classify_paths(paths) == "A"

    def test_github_workflow_is_b(self) -> None:
        paths = [".github/workflows/ci.yml"]
        assert classify_paths(paths) == "B"

    def test_yaml_in_github_is_b(self) -> None:
        # .yml/.yaml anywhere is class-B, even under .github/ non-workflow.
        paths = [".github/dependabot.yml"]
        assert classify_paths(paths) == "B"

    def test_md_files_are_a(self) -> None:
        paths = ["docs/security.md", "CHANGELOG.md", "frontend/README.md"]
        assert classify_paths(paths) == "A"

    def test_scripts_dir_is_b(self) -> None:
        paths = ["scripts/run_vulture.py"]
        assert classify_paths(paths) == "B"

    def test_deploy_dir_is_b(self) -> None:
        paths = ["deploy/fly/entrypoint.sh"]
        assert classify_paths(paths) == "B"

    def test_migrations_are_b(self) -> None:
        paths = ["backend/src/modulo/db/migrations/versions/0123_foo.py"]
        assert classify_paths(paths) == "B"

    def test_mixed_a_and_b(self) -> None:
        paths = [
            "backend/tests/unit/test_bar.py",
            "scripts/fast_lane_classify.py",  # class-B
            "docs/readme.md",
        ]
        assert classify_paths(paths) == "B"


# ---------------------------------------------------------------------------
# Exit-code contract: class-B exits non-zero, class-A exits 0
# ---------------------------------------------------------------------------

from unittest.mock import MagicMock, patch  # noqa: E402

from fast_lane_classify import main as classify_main  # noqa: E402


class TestMainExitCode:
    """Verify the exit-code contract that CI's continue-on-error depends on.

    The fast-lane-classify CI job uses ``continue-on-error: true`` so the
    workflow stays "success" for every PR.  The merge-queue reads the
    *job-level* check conclusion, which is "failure" when the script exits
    non-zero (class-B) and "success" when it exits 0 (class-A eligible).
    """

    @patch("fast_lane_classify.subprocess.run")
    def test_class_b_exits_nonzero(self, mock_run: MagicMock) -> None:
        """A class-B PR (backend/src/ change) must exit 1."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="backend/src/modulo/api/routes/pipelines.py\n",
            stderr="",
        )
        rc = classify_main(
            [
                "--pr-number",
                "42",
                "--head-sha",
                "abc123",
                "--repo",
                "farnalabs/modulo",
                "--base-ref",
                "origin/main",
            ]
        )
        assert rc == 1, "class-B must exit 1 (job-level check = failure)"

    @patch("fast_lane_classify.check_sha_pinning", return_value=(True, "SHA-pinned"))
    @patch("fast_lane_classify.check_no_test_weakening", return_value=(True, []))
    @patch("fast_lane_classify.check_cap", return_value=(True, "cap OK"))
    @patch("fast_lane_classify.subprocess.run")
    def test_class_a_with_label_exits_zero(
        self,
        mock_run: MagicMock,
        mock_cap: MagicMock,
        mock_weaken: MagicMock,
        mock_pin: MagicMock,
    ) -> None:
        """A class-A PR with the label and passing guardrails must exit 0."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="backend/tests/unit/test_foo.py\n",
            stderr="",
        )
        rc = classify_main(
            [
                "--pr-number",
                "42",
                "--head-sha",
                "abc123",
                "--repo",
                "farnalabs/modulo",
                "--base-ref",
                "origin/main",
                "--has-label",
            ]
        )
        assert rc == 0, "class-A with label and guardrails pass must exit 0"

    @patch("fast_lane_classify.subprocess.run")
    def test_class_a_without_label_exits_nonzero(self, mock_run: MagicMock) -> None:
        """A class-A PR without the label must exit 1 (standard lane)."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="backend/tests/unit/test_foo.py\n",
            stderr="",
        )
        rc = classify_main(
            [
                "--pr-number",
                "42",
                "--head-sha",
                "abc123",
                "--repo",
                "farnalabs/modulo",
                "--base-ref",
                "origin/main",
                # no --has-label
            ]
        )
        assert rc == 1, "class-A without label must exit 1 (standard lane)"
