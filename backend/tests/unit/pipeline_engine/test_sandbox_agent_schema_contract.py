"""FAR-901: sandbox schema contract wiring inside ``_sandbox_agent_impl``.

Covers the production behaviours added for the advisory schema contract:

  - ``MODULO_SCHEMA_DIR`` is set on the E2B sandbox at creation time
  - the contract files are uploaded under ``/home/user/schemas/<node_id>/``
  - (llm mode only) the schema paths are injected into the rendered prompt
  - a contract write failure is best-effort and never fails the node

plus the ``_resolve_provider_id_from_agent`` helper that feeds the
``provider-strict`` renderer. Uses the established fake ``AsyncSandbox.create``
mock style (see ``test_sandbox_agent_workspace_inputs.py``).
"""

import uuid
from collections.abc import Callable
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.node_runner import (
    _resolve_provider_id_from_agent,
    make_sandbox_agent_fn,
)
from modulo.core.schema_registry.contract import ContractWriteResult

_ORG_ID = str(uuid.UUID("11111111-2222-3333-4444-555555555555"))
_AGENT_ID = str(uuid.uuid4())

_INPUT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"task": {"type": "string"}}}
_OUTPUT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"result": {"type": "string"}}}


@pytest.fixture(autouse=True)
def _remote_e2b_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Script mode requires a remote E2B provider (same seam as sibling tests)."""
    monkeypatch.setenv("MODULO_E2B_API_KEY", "test-e2b-key")


def _read_router(output_json: str) -> Callable[..., str]:
    def _read(path: str, format: str = "text", **kwargs: Any) -> str:
        if str(path).endswith("output.json"):
            return output_json
        return ""

    return _read


def _sandbox_mock(*, output_json: str = '{"result": "ok"}') -> MagicMock:
    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "agent stdout"
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(side_effect=_read_router(output_json))
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()
    sandbox.get_metrics = AsyncMock(return_value=MagicMock(cpu_used_pct=1.0, mem_used=1, disk_used=1))
    return sandbox


def _run_state() -> dict[str, Any]:
    return {
        "run_context": {"input": {"task": "x"}},
        "_run_id": str(uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")),
        "_pipeline_id": "pipe-1",
        "_org_id": _ORG_ID,
    }


def _script_node_def(**overrides: Any) -> dict[str, Any]:
    node_def: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "position": {"x": 0, "y": 0},
        "template_id": "opencode",
        "mode": "script",
        "script_command": "python3 /home/user/main.py",
        "agent_id": _AGENT_ID,
        "env_vars": {"GH_TOKEN": "node-token"},
        "input_schema_json": _INPUT_SCHEMA,
        "output_schema_json": _OUTPUT_SCHEMA,
    }
    node_def.update(overrides)
    return node_def


def _llm_node_def(**overrides: Any) -> dict[str, Any]:
    node_def: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "node_type": "sandbox_agent",
        "position": {"x": 0, "y": 0},
        "template_id": "opencode",
        "agent_prompt": "Do the thing",
        "agent_commands": ["opencode run --auto --format json < /home/user/prompt.md"],
        "agent_id": _AGENT_ID,
        "env_vars": {"GH_TOKEN": "node-token"},
        "input_schema_json": _INPUT_SCHEMA,
        "output_schema_json": _OUTPUT_SCHEMA,
    }
    node_def.update(overrides)
    return node_def


def _written_paths(sandbox: MagicMock) -> list[str]:
    return [call.args[0] for call in sandbox.files.write.await_args_list]


def _written_content(sandbox: MagicMock, path_suffix: str) -> str | None:
    for call in sandbox.files.write.await_args_list:
        if str(call.args[0]).endswith(path_suffix):
            return call.args[1]
    return None


async def test_script_mode_uploads_contract_files_and_sets_env() -> None:
    """Script mode sets MODULO_SCHEMA_DIR and uploads all four contract files."""
    node_def = _script_node_def()
    node_id = node_def["id"]
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _sandbox_mock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)) as create_mock,
        patch("modulo.core.runner_bindings.resolve_agent_bindings", new=AsyncMock(return_value={})),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    assert create_mock.call_args.kwargs["envs"]["MODULO_SCHEMA_DIR"] == "/home/user/schemas"

    paths = _written_paths(sandbox)
    assert f"/home/user/schemas/{node_id}/input.canonical.json" in paths
    assert f"/home/user/schemas/{node_id}/input.active.json" in paths
    assert f"/home/user/schemas/{node_id}/output.canonical.json" in paths
    assert f"/home/user/schemas/{node_id}/output.active.json" in paths
    # Script mode never writes a prompt, so no path injection can occur.
    assert "/home/user/prompt.md" not in paths


async def test_llm_mode_injects_schema_paths_into_prompt() -> None:
    """LLM mode appends the schema paths + env var to the rendered prompt."""
    node_def = _llm_node_def()
    node_id = node_def["id"]
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _sandbox_mock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.runner_bindings.resolve_agent_bindings", new=AsyncMock(return_value={})),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    prompt = _written_content(sandbox, "/home/user/prompt.md")
    assert prompt is not None
    assert "Schema contract files (advisory" in prompt
    assert f"{node_id}/input.active.json" in prompt
    assert f"{node_id}/output.active.json" in prompt
    assert "MODULO_SCHEMA_DIR=/home/user/schemas" in prompt


async def test_contract_write_failure_is_best_effort() -> None:
    """A contract write failure is logged and never fails the node."""
    node_def = _script_node_def()
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _sandbox_mock()

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.runner_bindings.resolve_agent_bindings", new=AsyncMock(return_value={})),
        patch(
            "modulo.core.pipeline_engine.node_runner.write_schema_contract",
            side_effect=RuntimeError("disk full"),
        ),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"
    assert not [path for path in _written_paths(sandbox) if "/home/user/schemas/" in path]


async def test_contract_write_warnings_are_logged_but_do_not_fail() -> None:
    """Warnings returned by the contract writer are advisory, never fatal."""
    node_def = _script_node_def()
    fn = make_sandbox_agent_fn(node_def)
    sandbox = _sandbox_mock()
    writer_result = ContractWriteResult(
        schema_files_written=False,
        warnings=["input:const_string_exceeds_256_chars"],
    )

    with (
        patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)),
        patch("modulo.core.runner_bindings.resolve_agent_bindings", new=AsyncMock(return_value={})),
        patch(
            "modulo.core.pipeline_engine.node_runner.write_schema_contract",
            return_value=writer_result,
        ),
    ):
        result = await fn(_run_state())

    assert result["output"]["status"] == "completed"


class _FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class _FakeBegin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    def __init__(self, results: list[_FakeResult]) -> None:
        self._results = results

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self) -> _FakeBegin:
        return _FakeBegin()

    async def execute(self, *args: Any, **kwargs: Any) -> _FakeResult:
        return self._results.pop(0)


def _factory(results: list[_FakeResult]) -> Callable[[], _FakeSession]:
    def _make() -> _FakeSession:
        return _FakeSession(results)

    return _make


class _Agent:
    def __init__(self, model_backend_id: Any) -> None:
        self.model_backend_id = model_backend_id


class _Backend:
    def __init__(self, provider: Any) -> None:
        self.provider = provider


async def test_resolve_provider_id_happy_path_lowercases() -> None:
    factory = _factory([_FakeResult(_Agent("mb-1")), _FakeResult(_Backend("OpenAI"))])
    assert await _resolve_provider_id_from_agent(factory, uuid.uuid4()) == "openai"


async def test_resolve_provider_id_returns_none_without_factory_or_agent() -> None:
    assert await _resolve_provider_id_from_agent(None, uuid.uuid4()) is None
    assert await _resolve_provider_id_from_agent(_factory([]), None) is None


@pytest.mark.parametrize(
    "results",
    [
        [_FakeResult(None)],
        [_FakeResult(_Agent(None))],
        [_FakeResult(_Agent("mb-1")), _FakeResult(None)],
        [_FakeResult(_Agent("mb-1")), _FakeResult(_Backend(None))],
    ],
)
async def test_resolve_provider_id_returns_none_for_missing_rows(results: list[_FakeResult]) -> None:
    assert await _resolve_provider_id_from_agent(_factory(results), uuid.uuid4()) is None


async def test_resolve_provider_id_swallows_db_error() -> None:
    def _boom() -> None:
        raise RuntimeError("db down")

    assert await _resolve_provider_id_from_agent(_boom, uuid.uuid4()) is None
