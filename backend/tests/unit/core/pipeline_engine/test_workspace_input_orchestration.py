"""Tests for FAR-800 workspace input orchestration (node_runner integration).

Covers:
  - Host-side ref resolution (ls-remote, retry, transient vs permanent)
  - In-sandbox provisioning (credential setup, clone, teardown, short-circuit)
  - Post-agent drift detection (SHA match, mismatch, detection failure)
  - Transient vs permanent error classification
  - Envelope integration (workspace_drift, workspace_drift_detected fields)
  - Gap 1: Connector-backed inputs without explicit URL
  - Gap 2: Read-only credential assertion wiring
"""

from __future__ import annotations

import uuid
from typing import Any, Self
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
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc_mock),
            pytest.raises(ConnectionError, match="git ls-remote failed"),
        ):
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

        with (
            patch(
                "modulo.core.pipeline_engine.workspace_input_orchestration._run_git_ls_remote",
                new_callable=AsyncMock,
                return_value=_FAKE_LS_REMOTE_OUTPUT,
            ),
            pytest.raises(RefResolutionError, match="not found"),
        ):
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

        with (
            patch(
                "modulo.core.pipeline_engine.workspace_input_orchestration._run_git_ls_remote",
                side_effect=_always_failing,
            ),
            pytest.raises(ConnectionError),
        ):
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
        with (
            patch(
                "modulo.core.pipeline_engine.workspace_input_orchestration._run_git_ls_remote",
                new_callable=AsyncMock,
                return_value=_FAKE_LS_REMOTE_OUTPUT,
            ),
            pytest.raises(ProvisioningError, match="resolution failed") as exc_info,
        ):
            await resolve_managed_inputs_host_side(inputs, org_id="org-1")
        assert exc_info.value.error_code == "sandbox.input_resolution_failed"
        assert exc_info.value.retryable is False

    @pytest.mark.asyncio
    async def test_network_error_raises_transient(self) -> None:
        inputs = [_make_input(kind="branch", value="main")]
        with (
            patch(
                "modulo.core.pipeline_engine.workspace_input_orchestration._run_git_ls_remote",
                new_callable=AsyncMock,
                side_effect=ConnectionError("network down"),
            ),
            pytest.raises(ProvisioningError, match="transient") as exc_info,
        ):
            await resolve_managed_inputs_host_side(inputs, org_id="org-1", max_retries=0)
        assert exc_info.value.error_code == "sandbox.input_resolution_failed"
        assert exc_info.value.retryable is True

    @pytest.mark.asyncio
    async def test_no_url_no_connector_raises(self) -> None:
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
                raise RuntimeError("teardown failed")
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
        assert not results[0].final_sha  # unknown

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


# ---------------------------------------------------------------------------
# Coverage for branches left unexercised by the happy-path tests above:
# empty-SHA guard, non-transient ref-retry error, the connector credential
# resolution path inside resolve_managed_inputs_host_side (incl. its error
# classification branches), and the unexpected-ref-resolution error branch.
# ---------------------------------------------------------------------------


def test_is_sha_empty_returns_false() -> None:
    """An empty / whitespace-only value is not a SHA (guard before length check)."""
    assert _is_sha("") is False
    assert _is_sha("   ") is False


async def test_resolve_ref_with_retry_non_transient_raises_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-transient exception (not ConnectionError/Timeout/OSError) during
    ls-remote must propagate immediately without retry (FAR-800 line 190)."""

    async def _boom(_url: str) -> str:
        raise ValueError("malformed url")

    monkeypatch.setattr("modulo.core.pipeline_engine.workspace_input_orchestration._run_git_ls_remote", _boom)
    with pytest.raises(ValueError, match="malformed url"):
        await _resolve_ref_with_retry("https://github.com/o/r.git", "branch", "main")


async def test_resolve_managed_inputs_unexpected_ref_error_is_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-RefResolution, non-transient error during ref resolution must raise
    a permanent (retryable=False) ProvisioningError (FAR-800 line 274)."""

    async def _boom(_url: str, _kind: str, _value: str, *, max_retries: int = 2) -> str:
        raise RuntimeError("unexpected git failure")

    monkeypatch.setattr("modulo.core.pipeline_engine.workspace_input_orchestration._resolve_ref_with_retry", _boom)
    with pytest.raises(ProvisioningError) as exc:
        await resolve_managed_inputs_host_side(
            [{"url": "https://github.com/o/r.git", "dest": "/home/user/r", "ref": {"kind": "branch", "value": "main"}}],
            org_id="org-1",
        )
    assert exc.value.retryable is False


class _FakeBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    def begin(self) -> _FakeBegin:
        return _FakeBegin()


class _Factory:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> _FakeSession:
        return self.session

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _make_factory() -> _Factory:
    return _Factory(_FakeSession())


async def test_resolve_managed_inputs_connector_credential_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """When workspace_inputs carry a connector_instance_id and a session_factory
    is supplied, the host-side credential resolution path runs and the resulting
    credential scripts are threaded onto the ResolvedInput (FAR-800 lines 286-322)."""
    fake_cred = MagicMock()
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.resolve_clone_credential",
        AsyncMock(return_value=fake_cred),
    )
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.build_provisioning_credential_scripts",
        MagicMock(return_value=("CRED_SETUP", "CRED_TEARDOWN")),
    )
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_orchestration._resolve_ref_with_retry",
        AsyncMock(return_value="a" * 40),
    )
    # Stub the read-only assertion to pass (SSH key → auto-accepted).
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.assert_clone_credential_is_read_only",
        AsyncMock(),
    )
    factory = _make_factory
    resolved = await resolve_managed_inputs_host_side(
        [{"url": "https://github.com/o/r.git", "dest": "/home/user/r", "connector_instance_id": str(uuid.uuid4())}],
        org_id="org-1",
        session_factory=factory,
    )
    assert len(resolved) == 1
    assert resolved[0].credential_setup_script == "CRED_SETUP"
    assert resolved[0].credential_teardown_script == "CRED_TEARDOWN"


async def test_resolve_managed_inputs_credential_resolution_error_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A CredentialResolutionError during credential resolution maps to a
    permanent (retryable=False) ProvisioningError (FAR-800 lines 298-303)."""
    from modulo.core.pipeline_engine.workspace_input_credentials import CredentialResolutionError

    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.resolve_clone_credential",
        AsyncMock(side_effect=CredentialResolutionError("no such connector")),
    )
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.build_provisioning_credential_scripts",
        MagicMock(return_value=("", "")),
    )
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_orchestration._resolve_ref_with_retry",
        AsyncMock(return_value="a" * 40),
    )
    factory = _make_factory
    with pytest.raises(ProvisioningError) as exc:
        await resolve_managed_inputs_host_side(
            [{"url": "https://github.com/o/r.git", "dest": "/home/user/r", "connector_instance_id": str(uuid.uuid4())}],
            org_id="org-1",
            session_factory=factory,
        )
    assert exc.value.error_code == "sandbox.input_credential_failed"
    assert exc.value.retryable is False


async def test_resolve_managed_inputs_credential_transient_error_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient credential-resolution error classifies as retryable (FAR-800 lines 304-310)."""
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.resolve_clone_credential",
        AsyncMock(side_effect=ConnectionError("db down")),
    )
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.build_provisioning_credential_scripts",
        MagicMock(return_value=("", "")),
    )
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_orchestration._resolve_ref_with_retry",
        AsyncMock(return_value="a" * 40),
    )
    factory = _make_factory
    with pytest.raises(ProvisioningError) as exc:
        await resolve_managed_inputs_host_side(
            [{"url": "https://github.com/o/r.git", "dest": "/home/user/r", "connector_instance_id": str(uuid.uuid4())}],
            org_id="org-1",
            session_factory=factory,
        )
    assert exc.value.error_code == "sandbox.input_credential_failed"
    assert exc.value.retryable is True


async def test_resolve_managed_inputs_credential_unexpected_error_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-transient, non-CredentialResolution error during credential
    resolution classifies as permanent (FAR-800 lines 311-315)."""
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.resolve_clone_credential",
        AsyncMock(side_effect=ValueError("unexpected")),
    )
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.build_provisioning_credential_scripts",
        MagicMock(return_value=("", "")),
    )
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_orchestration._resolve_ref_with_retry",
        AsyncMock(return_value="a" * 40),
    )
    factory = _make_factory
    with pytest.raises(ProvisioningError) as exc:
        await resolve_managed_inputs_host_side(
            [{"url": "https://github.com/o/r.git", "dest": "/home/user/r", "connector_instance_id": str(uuid.uuid4())}],
            org_id="org-1",
            session_factory=factory,
        )
    assert exc.value.error_code == "sandbox.input_credential_failed"
    assert exc.value.retryable is False


# ---------------------------------------------------------------------------
# Gap 1: Connector-backed inputs without explicit URL
# ---------------------------------------------------------------------------


class _FakeCi:
    """Minimal ConnectorInstance stand-in for URL-derivation tests."""

    def __init__(self, connector_type_id: str, config_json: dict[str, Any]) -> None:
        self.id = uuid.uuid4()
        self.connector_type_id = connector_type_id
        self.config_json = config_json


class _FakeResult:
    def __init__(self, ci: _FakeCi | None) -> None:
        self._ci = ci

    def scalar_one_or_none(self) -> _FakeCi | None:
        return self._ci


class _FakeSessionForUrl:
    def __init__(self, ci: _FakeCi | None) -> None:
        self._ci = ci

    async def execute(self, _stmt: Any) -> _FakeResult:
        return _FakeResult(self._ci)

    def begin(self) -> _FakeBegin:
        return _FakeBegin()


class _FakeSessionCtxForUrl:
    def __init__(self, ci: _FakeCi | None) -> None:
        self._ci = ci

    async def __aenter__(self) -> _FakeSessionForUrl:
        return _FakeSessionForUrl(self._ci)

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self) -> _FakeBegin:
        return _FakeBegin()


class _FactoryForUrl:
    def __init__(self, ci: _FakeCi | None) -> None:
        self._ci = ci

    def __call__(self) -> _FakeSessionCtxForUrl:
        return _FakeSessionCtxForUrl(self._ci)


async def test_connector_backed_input_derives_url_from_github_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connector-backed input without URL derives the clone URL from the
    GitHub connector's stored config (repo + base_url) — FAR-800 follow-up Gap 1."""
    fake_ci = _FakeCi("github", {"repo": "org/repo", "base_url": "https://api.github.com"})
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_orchestration._resolve_ref_with_retry",
        AsyncMock(return_value="a" * 40),
    )
    # Also mock credential resolution (the connector_instance_id triggers it).
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.resolve_clone_credential",
        AsyncMock(return_value=None),
    )
    factory = _FactoryForUrl(fake_ci)
    resolved = await resolve_managed_inputs_host_side(
        [
            {
                "dest": "/home/user/repo",
                "connector_instance_id": str(uuid.uuid4()),
                "ref": {"kind": "branch", "value": "main"},
            }
        ],
        org_id="org-1",
        session_factory=factory,
    )
    assert len(resolved) == 1
    assert resolved[0].url == "https://github.com/org/repo.git"


async def test_connector_backed_input_strips_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trailing slash on the repo config key is stripped before appending .git."""
    fake_ci = _FakeCi("github", {"repo": "org/repo/", "base_url": "https://api.github.com"})
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_orchestration._resolve_ref_with_retry",
        AsyncMock(return_value="a" * 40),
    )
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.resolve_clone_credential",
        AsyncMock(return_value=None),
    )
    factory = _FactoryForUrl(fake_ci)
    resolved = await resolve_managed_inputs_host_side(
        [
            {
                "dest": "/home/user/repo",
                "connector_instance_id": str(uuid.uuid4()),
                "ref": {"kind": "branch", "value": "main"},
            }
        ],
        org_id="org-1",
        session_factory=factory,
    )
    assert resolved[0].url == "https://github.com/org/repo.git"


async def test_connector_backed_input_no_connector_raises() -> None:
    """An input with no URL and no connector_instance_id raises."""
    with pytest.raises(ProvisioningError, match="no connector_instance_id"):
        await resolve_managed_inputs_host_side(
            [{"dest": "/home/user/repo", "ref": {"kind": "branch", "value": "main"}}],
            org_id="org-1",
        )


async def test_connector_backed_input_no_session_factory_raises() -> None:
    """An input with no URL but a connector_instance_id and no session_factory raises."""
    with pytest.raises(ProvisioningError, match="session_factory"):
        await resolve_managed_inputs_host_side(
            [
                {
                    "dest": "/home/user/repo",
                    "connector_instance_id": str(uuid.uuid4()),
                    "ref": {"kind": "branch", "value": "main"},
                }
            ],
            org_id="org-1",
            session_factory=None,
        )


async def test_connector_backed_input_connector_not_found_raises() -> None:
    """When the connector instance is not in the DB, raises ProvisioningError."""
    factory = _FactoryForUrl(None)  # None = connector not found
    with pytest.raises(ProvisioningError, match="not found"):
        await resolve_managed_inputs_host_side(
            [
                {
                    "dest": "/home/user/repo",
                    "connector_instance_id": str(uuid.uuid4()),
                    "ref": {"kind": "branch", "value": "main"},
                }
            ],
            org_id="org-1",
            session_factory=factory,
        )


async def test_connector_backed_input_unsupported_type_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connector type without URL derivation raises ProvisioningError."""
    fake_ci = _FakeCi("slack", {"webhook_url": "https://hooks.slack.com/..."})
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_orchestration._resolve_ref_with_retry",
        AsyncMock(return_value="a" * 40),
    )
    factory = _FactoryForUrl(fake_ci)
    with pytest.raises(ProvisioningError, match="does not support URL derivation"):
        await resolve_managed_inputs_host_side(
            [
                {
                    "dest": "/home/user/repo",
                    "connector_instance_id": str(uuid.uuid4()),
                    "ref": {"kind": "branch", "value": "main"},
                }
            ],
            org_id="org-1",
            session_factory=factory,
        )


async def test_connector_backed_input_derives_url_ghe_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A GitHub Enterprise base_url reuses the same host for the clone URL."""
    fake_ci = _FakeCi("github", {"repo": "acme/widgets", "base_url": "https://ghe.acme.com/api/v3"})
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_orchestration._resolve_ref_with_retry",
        AsyncMock(return_value="a" * 40),
    )
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.resolve_clone_credential",
        AsyncMock(return_value=None),
    )
    factory = _FactoryForUrl(fake_ci)
    resolved = await resolve_managed_inputs_host_side(
        [
            {
                "dest": "/home/user/repo",
                "connector_instance_id": str(uuid.uuid4()),
                "ref": {"kind": "branch", "value": "main"},
            }
        ],
        org_id="org-1",
        session_factory=factory,
    )
    assert resolved[0].url == "https://ghe.acme.com/acme/widgets.git"


async def test_connector_backed_input_missing_repo_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A github connector without a 'repo' key cannot derive a clone URL."""
    fake_ci = _FakeCi("github", {"base_url": "https://api.github.com"})
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_orchestration._resolve_ref_with_retry",
        AsyncMock(return_value="a" * 40),
    )
    factory = _FactoryForUrl(fake_ci)
    with pytest.raises(ProvisioningError, match="no 'repo'"):
        await resolve_managed_inputs_host_side(
            [
                {
                    "dest": "/home/user/repo",
                    "connector_instance_id": str(uuid.uuid4()),
                    "ref": {"kind": "branch", "value": "main"},
                }
            ],
            org_id="org-1",
            session_factory=factory,
        )


# ---------------------------------------------------------------------------
# Gap 2: Read-only credential assertion
# ---------------------------------------------------------------------------


async def test_read_only_credential_assertion_called_when_http_client_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When http_client is provided, assert_clone_credential_is_read_only is called
    during credential resolution (FAR-800 follow-up Gap 2)."""
    fake_cred = MagicMock()
    fake_cred.kind = "ssh"
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.resolve_clone_credential",
        AsyncMock(return_value=fake_cred),
    )
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.build_provisioning_credential_scripts",
        MagicMock(return_value=("SETUP", "TEARDOWN")),
    )
    mock_assert = AsyncMock()
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.assert_clone_credential_is_read_only",
        mock_assert,
    )
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_orchestration._resolve_ref_with_retry",
        AsyncMock(return_value="a" * 40),
    )
    fake_http = MagicMock()
    factory = _make_factory
    resolved = await resolve_managed_inputs_host_side(
        [{"url": "https://github.com/o/r.git", "dest": "/home/user/r", "connector_instance_id": str(uuid.uuid4())}],
        org_id="org-1",
        session_factory=factory,
        http_client=fake_http,
    )
    assert len(resolved) == 1
    mock_assert.assert_called_once_with(fake_cred, http_client=fake_http)


async def test_read_only_credential_assertion_skipped_without_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When http_client is None, the assertion is NOT called (backward compat)."""
    fake_cred = MagicMock()
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.resolve_clone_credential",
        AsyncMock(return_value=fake_cred),
    )
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.build_provisioning_credential_scripts",
        MagicMock(return_value=("SETUP", "TEARDOWN")),
    )
    mock_assert = AsyncMock()
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.assert_clone_credential_is_read_only",
        mock_assert,
    )
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_orchestration._resolve_ref_with_retry",
        AsyncMock(return_value="a" * 40),
    )
    factory = _make_factory
    resolved = await resolve_managed_inputs_host_side(
        [{"url": "https://github.com/o/r.git", "dest": "/home/user/r", "connector_instance_id": str(uuid.uuid4())}],
        org_id="org-1",
        session_factory=factory,
        http_client=None,
    )
    assert len(resolved) == 1
    mock_assert.assert_not_called()


async def test_read_only_credential_assertion_failure_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the credential assertion fails (push-capable token), raises ProvisioningError."""
    from modulo.core.pipeline_engine.workspace_input_credentials import CredentialResolutionError

    fake_cred = MagicMock()
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.resolve_clone_credential",
        AsyncMock(return_value=fake_cred),
    )
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.build_provisioning_credential_scripts",
        MagicMock(return_value=("SETUP", "TEARDOWN")),
    )
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_credentials.assert_clone_credential_is_read_only",
        AsyncMock(side_effect=CredentialResolutionError("token is push-capable")),
    )
    monkeypatch.setattr(
        "modulo.core.pipeline_engine.workspace_input_orchestration._resolve_ref_with_retry",
        AsyncMock(return_value="a" * 40),
    )
    factory = _make_factory
    with pytest.raises(ProvisioningError, match="credential is not read-only") as exc:
        await resolve_managed_inputs_host_side(
            [{"url": "https://github.com/o/r.git", "dest": "/home/user/r", "connector_instance_id": str(uuid.uuid4())}],
            org_id="org-1",
            session_factory=factory,
            http_client=MagicMock(),
        )
    assert exc.value.error_code == "sandbox.input_credential_failed"
    assert exc.value.retryable is False
