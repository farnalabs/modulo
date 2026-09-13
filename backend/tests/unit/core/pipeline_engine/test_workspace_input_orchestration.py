"""Tests for FAR-800 workspace input orchestration (node_runner integration).

Covers:
  - Host-side ref resolution (ls-remote, retry, transient vs permanent)
  - In-sandbox provisioning (credential setup, clone, teardown, short-circuit)
  - Post-agent drift detection (SHA match, mismatch, detection failure)
  - Transient vs permanent error classification
  - Envelope integration (workspace_drift, workspace_drift_detected fields)
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.workspace_input_orchestration import (
    DriftResult,
    ProvisioningError,
    ResolvedInput,
    _extract_host_from_url,
    _is_sha,
    _is_transient_error,
    _resolve_ref_with_retry,
    _run_git_ls_remote,
    detect_workspace_input_drift,
    provision_workspace_inputs_in_sandbox,
    resolve_managed_inputs_host_side,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_input(
    *,
    url: str = "https://github.com/org/repo.git",
    dest: str = "/home/user/repo",
    kind: str = "branch",
    value: str = "main",
    connector_instance_id: Any = None,
) -> dict[str, Any]:
    """Build a workspace input dict."""
    inp: dict[str, Any] = {
        "url": url,
        "dest": dest,
        "ref": {"kind": kind, "value": value},
    }
    if connector_instance_id is not None:
        inp["connector_instance_id"] = connector_instance_id
    return inp


_FAKE_LS_REMOTE_OUTPUT = (
    "abc123def456abc123def456abc123def456abc1\trefs/heads/main\n"
    "def789abc012def789abc012def789abc012def7\trefs/heads/feature-x\n"
    "111222333444555666777888999000aaabbbccc\trefs/tags/v1.0\n"
    "222333444555666777888999000aaabbbcccddd\trefs/tags/v1.0^{}\n"
)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


class TestExtractHostFromUrl:
    def test_https(self) -> None:
        assert _extract_host_from_url("https://github.com/org/repo.git") == "github.com"

    def test_ssh(self) -> None:
        assert _extract_host_from_url("ssh://git@github.com/org/repo.git") == "github.com"

    def test_git_at(self) -> None:
        assert _extract_host_from_url("git@github.com:org/repo.git") == "github.com"

    def test_custom_port(self) -> None:
        assert _extract_host_from_url("https://myhost.example.com:8443/repo.git") == "myhost.example.com"

    def test_no_host_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot extract host"):
            _extract_host_from_url("")


class TestIsSha:
    def test_valid_40(self) -> None:
        assert _is_sha("abc123def456abc123def456abc123def456abc1")

    def test_valid_64(self) -> None:
        assert _is_sha("a" * 64)

    def test_invalid_length(self) -> None:
        assert not _is_sha("abc123")

    def test_invalid_chars(self) -> None:
        assert not _is_sha("xyz" * 14)

    def test_with_whitespace(self) -> None:
        assert _is_sha("  abc123def456abc123def456abc123def456abc1  ")


class TestIsTransientError:
    def test_connection_error(self) -> None:
        assert _is_transient_error(ConnectionError("timeout"))

    def test_timeout_error(self) -> None:
        assert _is_transient_error(TimeoutError("timed out"))

    def test_os_error(self) -> None:
        assert _is_transient_error(OSError("network unreachable"))

    def test_value_error_not_transient(self) -> None:
        assert not _is_transient_error(ValueError("bad input"))

    def test_exception_not_transient(self) -> None:
        assert not _is_transient_error(Exception("generic"))


# ---------------------------------------------------------------------------
# Host-side ref resolution
# ---------------------------------------------------------------------------


class TestRunGitLsRemote:
    @pytest.mark.asyncio
    async def test_success(self) -> None:
        proc_mock = AsyncMock()
        proc_mock.communicate = AsyncMock(return_value=(_FAKE_LS_REMOTE_OUTPUT.encode(), b""))
        proc_mock.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc_mock):
            result = await _run_git_ls_remote("https://github.com/org/repo.git")
        assert "refs/heads/main" in result

    @pytest.mark.asyncio
    async def test_failure_raises_connection_error(self) -> None:
        proc_mock = AsyncMock()
        proc_mock.communicate = AsyncMock(return_value=(b"", b"fatal: repository not found"))
        proc_mock.returncode = 128
        with patch("asyncio.create_subprocess_exec", return_value=proc_mock):
            with pytest.raises(ConnectionError, match="git ls-remote failed"):
                await _run_git_ls_remote("https://github.com/org/nonexistent.git")


class TestResolveRefWithRetry:
    @pytest.mark.asyncio
    async def test_sha_passthrough(self) -> None:
        """SHA refs don't need ls-remote."""
        result = await _resolve_ref_with_retry(
            "https://github.com/org/repo.git",
            "sha",
            "abc123def456abc123def456abc123def456abc1",
            max_retries=0,
        )
        assert result == "abc123def456abc123def456abc123def456abc1"

    @pytest.mark.asyncio
    async def test_branch_resolution(self) -> None:
        with patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration._run_git_ls_remote",
            new_callable=AsyncMock,
            return_value=_FAKE_LS_REMOTE_OUTPUT,
        ):
            result = await _resolve_ref_with_retry(
                "https://github.com/org/repo.git",
                "branch",
                "main",
                max_retries=0,
            )
        assert result == "abc123def456abc123def456abc123def456abc1"

    @pytest.mark.asyncio
    async def test_ref_not_found_raises(self) -> None:
        from modulo.core.pipeline_engine.workspace_inputs import RefResolutionError

        with patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration._run_git_ls_remote",
            new_callable=AsyncMock,
            return_value=_FAKE_LS_REMOTE_OUTPUT,
        ):
            with pytest.raises(RefResolutionError, match="not found"):
                await _resolve_ref_with_retry(
                    "https://github.com/org/repo.git",
                    "branch",
                    "nonexistent",
                    max_retries=2,
                )

    @pytest.mark.asyncio
    async def test_transient_error_retries(self) -> None:
        call_count = 0

        async def _failing_then_ok(url: str) -> str:
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ConnectionError("network error")
            return _FAKE_LS_REMOTE_OUTPUT

        with patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration._run_git_ls_remote",
            side_effect=_failing_then_ok,
        ):
            result = await _resolve_ref_with_retry(
                "https://github.com/org/repo.git",
                "branch",
                "main",
                max_retries=3,
            )
        assert result == "abc123def456abc123def456abc123def456abc1"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_transient_error_exhausts_retries(self) -> None:
        async def _always_failing(url: str) -> str:
            raise ConnectionError("network error")

        with patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration._run_git_ls_remote",
            side_effect=_always_failing,
        ):
            with pytest.raises(ConnectionError):
                await _resolve_ref_with_retry(
                    "https://github.com/org/repo.git",
                    "branch",
                    "main",
                    max_retries=2,
                )


# ---------------------------------------------------------------------------
# resolve_managed_inputs_host_side (integration)
# ---------------------------------------------------------------------------


class TestResolveManagedInputsHostSide:
    @pytest.mark.asyncio
    async def test_sha_passthrough_no_ls_remote(self) -> None:
        """SHA refs skip ls-remote entirely."""
        sha = "abc123def456abc123def456abc123def456abc1"
        inputs = [_make_input(kind="sha", value=sha)]
        result = await resolve_managed_inputs_host_side(inputs, org_id="org-1")
        assert len(result) == 1
        assert result[0].resolved_sha == sha

    @pytest.mark.asyncio
    async def test_branch_resolution(self) -> None:
        inputs = [_make_input(kind="branch", value="main")]
        with patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration._run_git_ls_remote",
            new_callable=AsyncMock,
            return_value=_FAKE_LS_REMOTE_OUTPUT,
        ):
            result = await resolve_managed_inputs_host_side(inputs, org_id="org-1")
        assert len(result) == 1
        assert result[0].resolved_sha == "abc123def456abc123def456abc123def456abc1"

    @pytest.mark.asyncio
    async def test_tag_resolution_peeled(self) -> None:
        """Annotated tags resolve to the peeled (underlying commit) SHA."""
        inputs = [_make_input(kind="tag", value="v1.0")]
        with patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration._run_git_ls_remote",
            new_callable=AsyncMock,
            return_value=_FAKE_LS_REMOTE_OUTPUT,
        ):
            result = await resolve_managed_inputs_host_side(inputs, org_id="org-1")
        assert result[0].resolved_sha == "222333444555666777888999000aaabbbcccddd"

    @pytest.mark.asyncio
    async def test_ref_not_found_raises_permanent(self) -> None:
        inputs = [_make_input(kind="branch", value="nonexistent")]
        with patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration._run_git_ls_remote",
            new_callable=AsyncMock,
            return_value=_FAKE_LS_REMOTE_OUTPUT,
        ):
            with pytest.raises(ProvisioningError, match="resolution failed") as exc_info:
                await resolve_managed_inputs_host_side(inputs, org_id="org-1")
        assert exc_info.value.error_code == "sandbox.input_resolution_failed"
        assert exc_info.value.retryable is False

    @pytest.mark.asyncio
    async def test_network_error_raises_transient(self) -> None:
        inputs = [_make_input(kind="branch", value="main")]
        with patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration._run_git_ls_remote",
            new_callable=AsyncMock,
            side_effect=ConnectionError("network down"),
        ):
            with pytest.raises(ProvisioningError, match="transient") as exc_info:
                await resolve_managed_inputs_host_side(inputs, org_id="org-1", max_retries=0)
        assert exc_info.value.error_code == "sandbox.input_resolution_failed"
        assert exc_info.value.retryable is True

    @pytest.mark.asyncio
    async def test_no_url_raises_permanent(self) -> None:
        inputs = [{"dest": "/home/user/repo", "ref": {"kind": "branch", "value": "main"}}]
        with pytest.raises(ProvisioningError, match="no url"):
            await resolve_managed_inputs_host_side(inputs, org_id="org-1")

    @pytest.mark.asyncio
    async def test_multiple_inputs(self) -> None:
        inputs = [
            _make_input(
                url="https://github.com/org/repo.git",
                dest="/home/user/repo",
                kind="sha",
                value="abc123def456abc123def456abc123def456abc1",
            ),
            _make_input(
                url="https://github.com/org/other.git",
                dest="/home/user/other",
                kind="branch",
                value="feature-x",
            ),
        ]
        with patch(
            "modulo.core.pipeline_engine.workspace_input_orchestration._run_git_ls_remote",
            new_callable=AsyncMock,
            return_value=_FAKE_LS_REMOTE_OUTPUT,
        ):
            result = await resolve_managed_inputs_host_side(inputs, org_id="org-1")
        assert len(result) == 2
        assert result[0].resolved_sha == "abc123def456abc123def456abc123def456abc1"
        assert result[1].resolved_sha == "def789abc012def789abc012def789abc012def7"


# ---------------------------------------------------------------------------
# Provisioning in sandbox
# ---------------------------------------------------------------------------


class TestProvisionWorkspaceInputsInSandbox:
    @pytest.mark.asyncio
    async def test_clone_success(self) -> None:
        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock()
        resolved = [
            ResolvedInput(
                url="https://github.com/org/repo.git",
                dest="/home/user/repo",
                resolved_sha="abc123",
            )
        ]
        await provision_workspace_inputs_in_sandbox(sandbox, resolved)
        # Clone script should have been called.
        assert sandbox.commands.run.call_count >= 1

    @pytest.mark.asyncio
    async def test_credential_setup_teardown(self) -> None:
        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock()
        resolved = [
            ResolvedInput(
                url="https://github.com/org/repo.git",
                dest="/home/user/repo",
                resolved_sha="abc123",
                credential_setup_script="#!/bin/sh\nsetup\n",
                credential_teardown_script="#!/bin/sh\ntearardown\n",
            )
        ]
        await provision_workspace_inputs_in_sandbox(sandbox, resolved)
        # setup + clone + teardown = 3 calls.
        assert sandbox.commands.run.call_count == 3

    @pytest.mark.asyncio
    async def test_clone_failure_raises_provisioning_error(self) -> None:
        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock(side_effect=Exception("clone failed"))
        resolved = [
            ResolvedInput(
                url="https://github.com/org/repo.git",
                dest="/home/user/repo",
                resolved_sha="abc123",
            )
        ]
        with pytest.raises(ProvisioningError, match="clone failed") as exc_info:
            await provision_workspace_inputs_in_sandbox(sandbox, resolved)
        assert exc_info.value.error_code == "sandbox.input_checkout_failed"
        assert exc_info.value.retryable is False

    @pytest.mark.asyncio
    async def test_credential_setup_failure_raises(self) -> None:
        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock(side_effect=Exception("cred setup failed"))
        resolved = [
            ResolvedInput(
                url="https://github.com/org/repo.git",
                dest="/home/user/repo",
                resolved_sha="abc123",
                credential_setup_script="#!/bin/sh\nsetup\n",
            )
        ]
        with pytest.raises(ProvisioningError, match="credential setup failed"):
            await provision_workspace_inputs_in_sandbox(sandbox, resolved)

    @pytest.mark.asyncio
    async def test_teardown_failure_is_best_effort(self) -> None:
        """Teardown failure should NOT raise — it's best-effort."""
        call_count = 0

        async def _run_side_effect(script: str, **kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 3:  # teardown call
                raise Exception("teardown failed")
            return MagicMock()

        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock(side_effect=_run_side_effect)
        resolved = [
            ResolvedInput(
                url="https://github.com/org/repo.git",
                dest="/home/user/repo",
                resolved_sha="abc123",
                credential_setup_script="#!/bin/sh\nsetup\n",
                credential_teardown_script="#!/bin/sh\nteardown\n",
            )
        ]
        # Should NOT raise.
        await provision_workspace_inputs_in_sandbox(sandbox, resolved)

    @pytest.mark.asyncio
    async def test_empty_inputs_noop(self) -> None:
        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock()
        await provision_workspace_inputs_in_sandbox(sandbox, [])
        sandbox.commands.run.assert_not_called()


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


class TestDetectWorkspaceInputDrift:
    @pytest.mark.asyncio
    async def test_no_drift(self) -> None:
        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock(return_value=MagicMock(stdout="abc123\n"))
        resolved = [
            ResolvedInput(
                url="https://github.com/org/repo.git",
                dest="/home/user/repo",
                resolved_sha="abc123",
            )
        ]
        results = await detect_workspace_input_drift(sandbox, resolved)
        assert len(results) == 1
        assert results[0].drift_detected is False
        assert results[0].final_sha == "abc123"

    @pytest.mark.asyncio
    async def test_drift_detected(self) -> None:
        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock(return_value=MagicMock(stdout="deadbeef\n"))
        resolved = [
            ResolvedInput(
                url="https://github.com/org/repo.git",
                dest="/home/user/repo",
                resolved_sha="abc123",
            )
        ]
        results = await detect_workspace_input_drift(sandbox, resolved)
        assert len(results) == 1
        assert results[0].drift_detected is True
        assert results[0].final_sha == "deadbeef"

    @pytest.mark.asyncio
    async def test_detection_failure_marks_drift(self) -> None:
        """Detection failure → fail-closed (drift_detected=True)."""
        sandbox = MagicMock()
        sandbox.commands.run = AsyncMock(side_effect=Exception("sandbox dead"))
        resolved = [
            ResolvedInput(
                url="https://github.com/org/repo.git",
                dest="/home/user/repo",
                resolved_sha="abc123",
            )
        ]
        results = await detect_workspace_input_drift(sandbox, resolved)
        assert len(results) == 1
        assert results[0].drift_detected is True  # fail-closed
        assert results[0].final_sha == ""  # unknown

    @pytest.mark.asyncio
    async def test_empty_inputs(self) -> None:
        sandbox = MagicMock()
        results = await detect_workspace_input_drift(sandbox, [])
        assert results == []

    @pytest.mark.asyncio
    async def test_multiple_inputs(self) -> None:
        sandbox = MagicMock()
        call_count = 0

        async def _run_side_effect(script: str, **kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MagicMock(stdout="sha1\n")
            return MagicMock(stdout="sha2_mismatch\n")

        sandbox.commands.run = AsyncMock(side_effect=_run_side_effect)
        resolved = [
            ResolvedInput(url="https://github.com/a.git", dest="/home/user/a", resolved_sha="sha1"),
            ResolvedInput(url="https://github.com/b.git", dest="/home/user/b", resolved_sha="sha2"),
        ]
        results = await detect_workspace_input_drift(sandbox, resolved)
        assert len(results) == 2
        assert results[0].drift_detected is False
        assert results[1].drift_detected is True


# ---------------------------------------------------------------------------
# ProvisioningError
# ---------------------------------------------------------------------------


class TestProvisioningError:
    def test_default_fields(self) -> None:
        exc = ProvisioningError("test message")
        assert exc.message == "test message"
        assert exc.error_code == "sandbox.input_resolution_failed"
        assert exc.retryable is False

    def test_custom_fields(self) -> None:
        exc = ProvisioningError(
            "cred failed",
            error_code="sandbox.input_credential_failed",
            retryable=True,
        )
        assert exc.error_code == "sandbox.input_credential_failed"
        assert exc.retryable is True


# ---------------------------------------------------------------------------
# DriftResult
# ---------------------------------------------------------------------------


class TestDriftResult:
    def test_drift_result_fields(self) -> None:
        dr = DriftResult(
            dest="/home/user/repo",
            expected_sha="abc123",
            final_sha="deadbeef",
            drift_detected=True,
        )
        assert dr.dest == "/home/user/repo"
        assert dr.drift_detected is True
