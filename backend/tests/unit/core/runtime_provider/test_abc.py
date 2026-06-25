"""Unit tests for RuntimeProvider ABC and data classes."""

from modulo.core.runtime_provider import ExecResult, RuntimeProvider, WorkspaceSpec


def test_workspace_spec_defaults() -> None:
    import uuid

    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
    )
    assert spec.run_id is None
    assert spec.image_ref == ""
    assert spec.capabilities == []
    assert spec.timeout_seconds == 3600
    assert spec.resource_limits == {}
    assert spec.egress_policy is None
    assert spec.persistence_policy == {}
    assert spec.labels == {}


def test_exec_result_fields() -> None:
    result = ExecResult(exit_code=0, stdout="hello", stderr="", duration_ms=42)
    assert result.exit_code == 0
    assert result.stdout == "hello"
    assert result.duration_ms == 42


def test_exec_result_default_duration() -> None:
    result = ExecResult(exit_code=1, stdout="", stderr="error")
    assert result.duration_ms is None


def test_runtime_provider_cannot_instantiate() -> None:
    """RuntimeProvider is abstract and cannot be instantiated directly."""
    import pytest

    with pytest.raises(TypeError):
        RuntimeProvider()  # type: ignore[abstract]


def test_concrete_provider_must_implement_all_methods() -> None:
    """A subclass must implement all abstract methods."""
    import pytest

    class IncompleteProvider(RuntimeProvider):
        pass

    with pytest.raises(TypeError):
        IncompleteProvider()  # type: ignore[abstract]


async def test_concrete_provider_works() -> None:
    """A fully implemented provider can be instantiated and used."""

    class FakeProvider(RuntimeProvider):
        async def create_workspace(self, spec: WorkspaceSpec) -> str:
            return f"ws-{spec.environment_profile_id}"

        async def exec_command(
            self, provider_ref: str, command: list[str], *, timeout: int | None = None,  # noqa: ASYNC109
        ) -> ExecResult:
            return ExecResult(exit_code=0, stdout="ok", stderr="")

        async def destroy_workspace(self, provider_ref: str) -> None:
            pass

        async def get_workspace_status(self, provider_ref: str) -> str:
            return "running"

    import uuid

    provider = FakeProvider()
    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
    )

    ref = await provider.create_workspace(spec)
    assert ref.startswith("ws-")

    result = await provider.exec_command(ref, ["echo", "hi"])
    assert result.exit_code == 0

    await provider.destroy_workspace(ref)

    status = await provider.get_workspace_status(ref)
    assert status == "running"
