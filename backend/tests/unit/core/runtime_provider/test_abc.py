"""Unit tests for RuntimeProvider ABC and data classes."""

from modulo.core.runtime_provider import ExecResult, RuntimeProvider, WorkspaceSpec


def test_workspace_spec_defaults() -> None:
    import uuid

    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
    )
    assert spec.run_id is None
    assert not spec.image_ref
    assert not spec.capabilities
    assert spec.timeout_seconds == 3600
    assert not spec.resource_limits
    assert spec.egress_policy is None
    assert spec.persistence_policy == "ephemeral"
    assert not spec.labels
    assert not spec.workspace_metadata
    assert not spec.repo_url
    assert not spec.repo_ref


def test_workspace_spec_repo_fields_are_first_class() -> None:
    """FAR-595: clone inputs are first-class WorkspaceSpec fields.

    They were previously smuggled through the ``labels`` dict, which
    collided with Docker's labels-as-Env semantics.
    """
    import uuid

    spec = WorkspaceSpec(
        environment_profile_id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        repo_url="https://github.com/acme/app",
        repo_ref="develop",
    )
    assert spec.repo_url == "https://github.com/acme/app"
    assert spec.repo_ref == "develop"


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
            self,
            provider_ref: str,
            command: list[str],
            *,
            timeout: int | None = None,  # noqa: ASYNC109
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
