"""Unit tests for E2BRuntimeProvider with a mocked E2B SDK."""

from __future__ import annotations

import os
import uuid
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.runtime_provider import ExecResult, WorkspaceSpec
from modulo.core.runtime_provider.e2b import E2BRuntimeProvider


@pytest.fixture
def workspace_spec() -> WorkspaceSpec:
    return WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        image_ref="ubuntu-22.04",
    )


@pytest.fixture
def mock_sandbox() -> MagicMock:
    sbx = MagicMock()
    sbx.sandbox_id = "sbx-e2b-test-001"
    sbx.commands = MagicMock()
    sbx.commands.run = AsyncMock()
    return sbx


@pytest.fixture
def mock_sandbox_cls(mock_sandbox: MagicMock) -> Generator[MagicMock, None, None]:
    with patch("e2b.AsyncSandbox") as mock_cls:
        mock_cls.create = AsyncMock(return_value=mock_sandbox)
        yield mock_cls


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


def test_constructor_requires_api_key() -> None:
    with pytest.raises(ValueError, match="E2B API key is required"):
        E2BRuntimeProvider(api_key=None)


def test_constructor_accepts_explicit_key() -> None:
    provider = E2BRuntimeProvider(api_key="sk-explicit")
    assert provider._api_key == "sk-explicit"


def test_constructor_falls_back_to_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODULO_E2B_API_KEY", "sk-from-env")
    provider = E2BRuntimeProvider(api_key=None)
    assert provider._api_key == "sk-from-env"


def test_constructor_env_var_overrides_none() -> None:
    os.environ["MODULO_E2B_API_KEY"] = "sk-env"
    try:
        provider = E2BRuntimeProvider()
        assert provider._api_key == "sk-env"
    finally:
        del os.environ["MODULO_E2B_API_KEY"]


# ---------------------------------------------------------------------------
# supports()
# ---------------------------------------------------------------------------


class _DummyProfile:
    def __init__(self, hint: str = "", image_ref: str = ""):
        self.provider_hint = hint
        self.image_ref = image_ref


def test_supports_e2b_hint() -> None:
    provider = E2BRuntimeProvider(api_key="sk-test")
    assert provider.supports(_DummyProfile(hint="e2b")) is True


def test_supports_e2b_hint_case_insensitive() -> None:
    provider = E2BRuntimeProvider(api_key="sk-test")
    assert provider.supports(_DummyProfile(hint="E2B")) is True


def test_supports_e2b_in_image_ref() -> None:
    provider = E2BRuntimeProvider(api_key="sk-test")
    assert provider.supports(_DummyProfile(image_ref="my-e2b-template")) is True


def test_supports_other_hint() -> None:
    provider = E2BRuntimeProvider(api_key="sk-test")
    assert provider.supports(_DummyProfile(hint="docker")) is False


def test_supports_no_hint_no_image_ref() -> None:
    provider = E2BRuntimeProvider(api_key="sk-test")
    assert provider.supports(_DummyProfile()) is False


# ---------------------------------------------------------------------------
# create_workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workspace_creates_sandbox(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    provider = E2BRuntimeProvider(api_key="sk-test")
    ref = await provider.create_workspace(workspace_spec)

    assert ref == "sbx-e2b-test-001"
    mock_sandbox_cls.create.assert_called_once_with(template="ubuntu-22.04")


@pytest.mark.asyncio
async def test_create_workspace_default_template(mock_sandbox_cls: MagicMock) -> None:
    provider = E2BRuntimeProvider(api_key="sk-test")
    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        image_ref="",
    )
    await provider.create_workspace(spec)
    mock_sandbox_cls.create.assert_called_once_with(template="base")


@pytest.mark.asyncio
async def test_create_workspace_stores_sandbox(
    mock_sandbox_cls: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    provider = E2BRuntimeProvider(api_key="sk-test")
    ref = await provider.create_workspace(workspace_spec)
    assert ref in provider._sandboxes


@pytest.mark.asyncio
async def test_create_workspace_clones_repo(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
) -> None:
    mock_sandbox.commands.run.return_value = MagicMock(exit_code=0, stdout="", stderr="")

    provider = E2BRuntimeProvider(api_key="sk-test")
    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        labels={"repo_url": "https://github.com/user/repo.git"},
    )
    ref = await provider.create_workspace(spec)
    assert ref == "sbx-e2b-test-001"

    mock_sandbox.commands.run.assert_called_once()
    call_arg = mock_sandbox.commands.run.call_args[0][0]
    assert "git clone" in call_arg
    assert "https://github.com/user/repo.git" in call_arg


@pytest.mark.asyncio
async def test_create_workspace_checks_out_ref(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
) -> None:
    mock_sandbox.commands.run.return_value = MagicMock(exit_code=0, stdout="", stderr="")

    provider = E2BRuntimeProvider(api_key="sk-test")
    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        labels={
            "repo_url": "https://github.com/user/repo.git",
            "repo_ref": "develop",
        },
    )
    ref = await provider.create_workspace(spec)
    assert ref == "sbx-e2b-test-001"

    mock_sandbox.commands.run.assert_called_once()
    call_arg = mock_sandbox.commands.run.call_args[0][0]
    assert "git checkout develop" in call_arg


@pytest.mark.asyncio
async def test_create_workspace_skips_clone_when_no_repo_url(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    provider = E2BRuntimeProvider(api_key="sk-test")
    await provider.create_workspace(workspace_spec)
    mock_sandbox.commands.run.assert_not_called()


# ---------------------------------------------------------------------------
# exec_command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_command_returns_exec_result(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    proc_result = MagicMock()
    proc_result.exit_code = 0
    proc_result.stdout = "Python 3.12.0\n"
    proc_result.stderr = ""
    mock_sandbox.commands.run.return_value = proc_result

    provider = E2BRuntimeProvider(api_key="sk-test")
    ref = await provider.create_workspace(workspace_spec)
    result = await provider.exec_command(ref, ["python3", "--version"])

    assert isinstance(result, ExecResult)
    assert result.exit_code == 0
    assert result.stdout == "Python 3.12.0\n"


@pytest.mark.asyncio
async def test_exec_command_unknown_sandbox_raises(
    mock_sandbox_cls: MagicMock,
) -> None:
    provider = E2BRuntimeProvider(api_key="sk-test")
    with pytest.raises(ValueError, match="Unknown sandbox"):
        await provider.exec_command("nonexistent", ["echo", "hi"])


@pytest.mark.asyncio
async def test_exec_command_passes_timeout(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    proc_result = MagicMock(exit_code=0, stdout="", stderr="")
    mock_sandbox.commands.run.return_value = proc_result

    provider = E2BRuntimeProvider(api_key="sk-test")
    ref = await provider.create_workspace(workspace_spec)
    await provider.exec_command(ref, ["sleep", "1"], timeout=30)

    mock_sandbox.commands.run.assert_called_once()
    _args, kwargs = mock_sandbox.commands.run.call_args
    assert kwargs.get("timeout") == 30


@pytest.mark.asyncio
async def test_exec_command_non_zero_exit(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    proc_result = MagicMock()
    proc_result.exit_code = 127
    proc_result.stdout = ""
    proc_result.stderr = "command not found"
    mock_sandbox.commands.run.return_value = proc_result

    provider = E2BRuntimeProvider(api_key="sk-test")
    ref = await provider.create_workspace(workspace_spec)
    result = await provider.exec_command(ref, ["nonexistent"])

    assert result.exit_code == 127
    assert result.stderr == "command not found"


@pytest.mark.asyncio
async def test_exec_command_handles_proc_without_attributes(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    mock_sandbox.commands.run.return_value = object()

    provider = E2BRuntimeProvider(api_key="sk-test")
    ref = await provider.create_workspace(workspace_spec)
    result = await provider.exec_command(ref, ["echo", "hi"])

    assert result.exit_code == -1
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.asyncio
async def test_exec_command_default_timeout_when_none(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    proc_result = MagicMock(exit_code=0, stdout="", stderr="")
    mock_sandbox.commands.run.return_value = proc_result

    provider = E2BRuntimeProvider(api_key="sk-test")
    ref = await provider.create_workspace(workspace_spec)
    await provider.exec_command(ref, ["echo", "hi"], timeout=None)

    mock_sandbox.commands.run.assert_called_once()
    _args, kwargs = mock_sandbox.commands.run.call_args
    assert kwargs.get("timeout") == 60


@pytest.mark.asyncio
async def test_exec_command_shell_quoting(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    proc_result = MagicMock(exit_code=0, stdout="", stderr="")
    mock_sandbox.commands.run.return_value = proc_result

    provider = E2BRuntimeProvider(api_key="sk-test")
    ref = await provider.create_workspace(workspace_spec)
    await provider.exec_command(ref, ["echo", "hello world", "$VAR"])

    mock_sandbox.commands.run.assert_called_once()
    call_str = mock_sandbox.commands.run.call_args[0][0]
    assert "'hello world'" in call_str
    assert "'$VAR'" in call_str


# ---------------------------------------------------------------------------
# destroy_workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_destroy_workspace_kills_sandbox(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    provider = E2BRuntimeProvider(api_key="sk-test")
    ref = await provider.create_workspace(workspace_spec)
    await provider.destroy_workspace(ref)

    mock_sandbox.kill.assert_called_once()


@pytest.mark.asyncio
async def test_destroy_workspace_removes_from_dict(
    mock_sandbox_cls: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    provider = E2BRuntimeProvider(api_key="sk-test")
    ref = await provider.create_workspace(workspace_spec)
    await provider.destroy_workspace(ref)
    assert ref not in provider._sandboxes


@pytest.mark.asyncio
async def test_destroy_workspace_unknown_is_noop() -> None:
    provider = E2BRuntimeProvider(api_key="sk-test")
    await provider.destroy_workspace("nonexistent")  # should not raise


@pytest.mark.asyncio
async def test_destroy_workspace_handles_kill_failure(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    mock_sandbox.kill.side_effect = RuntimeError("E2B API error")

    provider = E2BRuntimeProvider(api_key="sk-test")
    ref = await provider.create_workspace(workspace_spec)
    # Should not raise — error is logged
    await provider.destroy_workspace(ref)
    assert ref not in provider._sandboxes


@pytest.mark.asyncio
async def test_destroy_workspace_kill_raises_type_error(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    mock_sandbox.kill.side_effect = TypeError("sandbox.kill() got unexpected argument")

    provider = E2BRuntimeProvider(api_key="sk-test")
    ref = await provider.create_workspace(workspace_spec)
    await provider.destroy_workspace(ref)
    assert ref not in provider._sandboxes


# ---------------------------------------------------------------------------
# get_workspace_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_workspace_status(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    mock_sandbox.is_running = AsyncMock(return_value=True)

    provider = E2BRuntimeProvider(api_key="sk-test")
    ref = await provider.create_workspace(workspace_spec)
    status = await provider.get_workspace_status(ref)
    assert status == "running"


@pytest.mark.asyncio
async def test_get_workspace_status_not_running(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    mock_sandbox.is_running = AsyncMock(return_value=False)

    provider = E2BRuntimeProvider(api_key="sk-test")
    ref = await provider.create_workspace(workspace_spec)
    status = await provider.get_workspace_status(ref)
    assert status == "stopped"


@pytest.mark.asyncio
async def test_get_workspace_status_fallback(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    del mock_sandbox.is_running

    provider = E2BRuntimeProvider(api_key="sk-test")
    ref = await provider.create_workspace(workspace_spec)
    status = await provider.get_workspace_status(ref)
    assert status == "unknown"


@pytest.mark.asyncio
async def test_get_workspace_status_unknown_sandbox() -> None:
    provider = E2BRuntimeProvider(api_key="sk-test")
    with pytest.raises(ValueError, match="Unknown sandbox"):
        await provider.get_workspace_status("nonexistent")


@pytest.mark.asyncio
async def test_get_workspace_status_is_running_raises(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    mock_sandbox.is_running = AsyncMock(side_effect=RuntimeError("API unavailable"))

    provider = E2BRuntimeProvider(api_key="sk-test")
    ref = await provider.create_workspace(workspace_spec)
    status = await provider.get_workspace_status(ref)
    assert status == "unknown"


@pytest.mark.asyncio
async def test_get_workspace_status_sandbox_without_is_running(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
    workspace_spec: WorkspaceSpec,
) -> None:
    mock_sandbox.is_running = None

    provider = E2BRuntimeProvider(api_key="sk-test")
    ref = await provider.create_workspace(workspace_spec)
    status = await provider.get_workspace_status(ref)
    assert status == "unknown"


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workspace_sandbox_constructor_raises() -> None:
    provider = E2BRuntimeProvider(api_key="sk-test")
    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        image_ref="bad-template",
    )
    with (
        patch("e2b.AsyncSandbox") as mock_cls,
        pytest.raises(Exception),
    ):
        mock_cls.create = AsyncMock(side_effect=RuntimeError("E2B API unavailable"))
        await provider.create_workspace(spec)


@pytest.mark.asyncio
async def test_create_workspace_clone_failure_propagates(
    mock_sandbox_cls: MagicMock,
    mock_sandbox: MagicMock,
) -> None:
    result = MagicMock()
    result.exit_code = 128
    result.stderr = "Host key verification failed"
    mock_sandbox.commands.run.return_value = result

    provider = E2BRuntimeProvider(api_key="sk-test")
    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        labels={"repo_url": "https://github.com/user/repo.git"},
    )
    with pytest.raises(RuntimeError, match="Repo clone failed"):
        await provider.create_workspace(spec)


@pytest.mark.asyncio
async def test_multiple_workspaces_independent(
    mock_sandbox_cls: MagicMock,
) -> None:
    provider = E2BRuntimeProvider(api_key="sk-test")

    sbx1 = MagicMock()
    sbx1.sandbox_id = "sbx-e2b-001"
    sbx1.commands = MagicMock()

    sbx2 = MagicMock()
    sbx2.sandbox_id = "sbx-e2b-002"
    sbx2.commands = MagicMock()

    mock_sandbox_cls.create = AsyncMock(side_effect=[sbx1, sbx2])

    spec1 = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        image_ref="template-a",
    )
    spec2 = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        image_ref="template-b",
    )

    ref1 = await provider.create_workspace(spec1)
    ref2 = await provider.create_workspace(spec2)

    assert ref1 == "sbx-e2b-001"
    assert ref2 == "sbx-e2b-002"
    assert len(provider._sandboxes) == 2


# ---------------------------------------------------------------------------
# Hub registration compatibility
# ---------------------------------------------------------------------------


def test_is_runtime_provider_subclass() -> None:
    from modulo.core.runtime_provider import RuntimeProvider

    assert issubclass(E2BRuntimeProvider, RuntimeProvider)


def test_register_with_hub() -> None:
    from modulo.core.runtime_provider.hub import RuntimeProviderHub

    hub = RuntimeProviderHub()
    provider = E2BRuntimeProvider(api_key="sk-test")
    hub.register("e2b", provider)
    assert hub.get("e2b") is provider
    assert hub.resolve(_DummyProfile(hint="e2b")) is provider
