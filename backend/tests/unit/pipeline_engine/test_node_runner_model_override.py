"""Unit tests for the FAR-332 run_context model_backend_id override.

The variant comparison and A/B test views emit a ``model_backend_id``
``run_context_overrides`` entry that is merged into the run's input payload.
The node runner must honour that override in preference to the
snapshot-embedded ``node_def["model_backend_id"]`` so every variant fires with
its own model backend.
"""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage


class _FakeBackend:
    """Fake model backend whose invoke() returns a fixed JSON payload."""

    def __init__(self, recorder: "_RecordingHub") -> None:
        self._recorder = recorder

    async def invoke(self, messages: list[Any], **kwargs: Any) -> AIMessage:
        self._recorder.prompts.append(messages[0].content)
        return AIMessage(content='{"ok": true}')


class _RecordingHub:
    """Fake ModelBackendHub that records every backend_id + prompt it was given."""

    def __init__(self) -> None:
        self.requested_ids: list[str] = []
        self.prompts: list[str] = []

    async def get(self, backend_id: uuid.UUID, **kwargs: Any) -> _FakeBackend:
        self.requested_ids.append(str(backend_id))
        return _FakeBackend(self)


async def _run_node(state: dict[str, Any]) -> tuple[dict[str, Any], _RecordingHub]:
    """Build an agent node and run it, returning the result and the recording hub."""
    from modulo.core.pipeline_engine.node_runner import make_node_fn

    node_def = {
        "id": "agent-1",
        "agent_id": "22222222-2222-2222-2222-222222222222",
        "prompt_template": "Summarise the input.",
        "model_backend_id": "11111111-1111-1111-1111-111111111111",
    }
    hub = _RecordingHub()
    node_fn = make_node_fn(node_def)
    with (
        patch(
            "modulo.core.pipeline_engine.node_runner.get_conformance_ctx",
            return_value=None,
        ),
        patch(
            "modulo.core.pipeline_engine.decorator.get_model_backend_hub",
            return_value=hub,
        ),
    ):
        result = await node_fn(state)
    return result, hub


@pytest.mark.asyncio
async def test_run_context_model_backend_override_wins_over_node_def() -> None:
    """The namespaced ``_run_overrides`` override is used, not the node_def backend.

    The executor seeds ``_run_overrides`` as a TOP-LEVEL run_context key from the
    run's frozen variant config — never inside ``input``.
    """
    override_backend = str(uuid.uuid4())
    state = {
        "run_context": {"input": {"task": "classify"}, "_run_overrides": {"model_backend_id": override_backend}},
        "artifacts": [],
    }
    result, hub = await _run_node(state)
    assert hub.requested_ids == [override_backend]
    assert result["artifacts"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_node_def_model_backend_used_when_no_override() -> None:
    """Without an override, the snapshot-embedded node_def backend is used."""
    state = {"run_context": {"input": {"task": "classify"}}, "artifacts": []}
    result, hub = await _run_node(state)
    assert hub.requested_ids == ["11111111-1111-1111-1111-111111111111"]
    assert result["artifacts"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_bare_data_model_backend_id_does_not_hijack() -> None:
    """A plain top-level ``model_backend_id`` data field must NOT reroute the model."""
    state = {
        "run_context": {"input": {"task": "classify", "model_backend_id": str(uuid.uuid4())}},
        "artifacts": [],
    }
    result, hub = await _run_node(state)
    assert hub.requested_ids == ["11111111-1111-1111-1111-111111111111"]
    assert result["artifacts"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_override_ignored_when_not_a_dict_input() -> None:
    """A non-dict input (string) cannot supply the override — falls back to node_def."""
    state = {"run_context": {"input": "free text prompt"}, "artifacts": []}
    result, hub = await _run_node(state)
    assert hub.requested_ids == ["11111111-1111-1111-1111-111111111111"]
    assert result["artifacts"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_prompt_template_override_wins_over_node_def() -> None:
    """A namespaced ``_run_overrides`` prompt_templates map is used for THIS node's agent.

    FAR-342: the variant comparison's prompt_version picker resolves a version
    label to a per-agent template map at run creation and stores it under
    ``_run_overrides["prompt_templates"]`` keyed by agent_id; the node runner
    must render the template for the node's OWN agent instead of the
    snapshot-embedded node_def prompt.
    """
    override_prompt = "Render THIS prompt version instead."
    state = {
        "run_context": {
            "input": {"task": "classify"},
            "_run_overrides": {"prompt_templates": {"22222222-2222-2222-2222-222222222222": override_prompt}},
        },
        "artifacts": [],
    }
    result, hub = await _run_node(state)
    assert hub.prompts == [override_prompt]
    assert result["artifacts"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_other_agent_prompt_override_does_not_clobber_this_node() -> None:
    """A prompt_templates override for a DIFFERENT agent must not apply here.

    FAR-342: in a multi-agent snapshot one agent's template must never clobber
    another's. This node's agent has no entry in the map, so it falls back to
    the node_def prompt.
    """
    other_agent_prompt = "This belongs to another agent."
    state = {
        "run_context": {
            "input": {"task": "classify"},
            "_run_overrides": {"prompt_templates": {"99999999-9999-9999-9999-999999999999": other_agent_prompt}},
        },
        "artifacts": [],
    }
    result, hub = await _run_node(state)
    assert hub.prompts == ["Summarise the input."]
    assert result["artifacts"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_node_def_prompt_used_when_no_prompt_override() -> None:
    """Without a prompt_templates override, the node_def prompt is rendered."""
    state = {"run_context": {"input": {"task": "classify"}}, "artifacts": []}
    result, hub = await _run_node(state)
    assert hub.prompts == ["Summarise the input."]
    assert result["artifacts"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_normal_run_caller_supplied_run_overrides_is_data_not_override() -> None:
    """FAR-342 injection: a NORMAL run's ``_run_overrides`` in input is DATA.

    A caller-supplied ``input_payload={"task": ..., "_run_overrides": {...}}``
    flows into ``run_context["input"]`` untouched — the executor only ever seeds
    the TOP-LEVEL ``_run_overrides`` key from the run's frozen variant config.
    With no top-level seed, the node_runner must render the snapshot prompt, NOT
    the injected one.
    """
    injected = "INJECTED prompt via caller input."
    state = {
        "run_context": {
            "input": {
                "task": "classify",
                "_run_overrides": {"prompt_templates": {"22222222-2222-2222-2222-222222222222": injected}},
            }
        },
        "artifacts": [],
    }
    result, hub = await _run_node(state)
    # The node_def prompt is rendered — the injected template in input never
    # reaches the override boundary.
    assert hub.prompts == ["Summarise the input."]
    assert result["artifacts"][0]["status"] == "completed"


# ---------------------------------------------------------------------------
# FAR-343: sandbox_agent per-run model variation via ``_run_overrides["model"]``
# ---------------------------------------------------------------------------


async def _run_sandbox_with_model_override(
    *,
    agent_command: str,
    override_model: str | None,
) -> str:
    """Run a sandbox_agent node with the given agent_command and model override.

    Returns the wrapped command actually dispatched to the E2B sandbox
    (``sandbox.commands.run.call_args.args[0]``).
    """
    from modulo.core.pipeline_engine.node_runner import make_sandbox_agent_fn

    node_def = {
        "id": "sbx-1",
        "agent_prompt": "Do the thing",
        "agent_command": agent_command,
    }
    fn = make_sandbox_agent_fn(node_def)

    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "agent stdout"
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(return_value='{"summary": "done"}')
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()

    state: dict[str, Any] = {
        "run_context": {"input": {"task": "x"}},
        "_run_id": "run-1",
        "_pipeline_id": "pipe-1",
        "_org_id": "org-1",
    }
    if override_model is not None:
        state["run_context"]["_run_overrides"] = {"model": override_model}

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(state)

    assert result["output"]["status"] == "completed"
    return sandbox.commands.run.call_args.args[0]


@pytest.mark.asyncio
async def test_sandbox_agent_command_renders_run_overrides_model() -> None:
    """A sandbox_agent command referencing ``_run_overrides.model`` varies per-run.

    FAR-343: the ``agent_command`` is Jinja-rendered with ``run_context`` in
    scope, and the executor seeds the frozen variant's ``model`` override into
    the TOP-LEVEL ``run_context["_run_overrides"]``. A pipeline author writes
    ``--model {{ run_context._run_overrides.model }}`` in the command to vary
    the opencode model per run.
    """
    wrapped = await _run_sandbox_with_model_override(
        agent_command="opencode run --model {{ run_context._run_overrides.model }} --auto < /home/user/prompt.md",
        override_model="opencode-go/hy3",
    )
    assert "opencode run --model opencode-go/hy3 --auto" in wrapped


@pytest.mark.asyncio
async def test_sandbox_agent_command_undefined_model_falls_back_verbatim() -> None:
    """Without a ``model`` override the ``{{ }}`` reference is undefined.

    The render path treats an undefined ``_run_overrides.model`` as a skipped
    node (UndefinedError) rather than injecting a broken model — a pipeline
    that references the override MUST supply it, otherwise the run is safely
    skipped rather than dispatched with a mangled command.
    """
    from modulo.core.pipeline_engine.node_runner import make_sandbox_agent_fn

    node_def = {
        "id": "sbx-2",
        "agent_prompt": "Do the thing",
        "agent_command": "opencode run --model {{ run_context._run_overrides.model }} --auto < /home/user/prompt.md",
    }
    fn = make_sandbox_agent_fn(node_def)
    sandbox = MagicMock()

    state = {
        "run_context": {"input": {"task": "x"}},
        "_run_id": "run-1",
        "_pipeline_id": "pipe-1",
        "_org_id": "org-1",
    }
    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(state)

    assert result["status"] == "skipped"
    assert "agent_command template references missing input fields" in result["summary"]


# ---------------------------------------------------------------------------
# FAR-436: node capability_scope.context_scope allowlists the run_context view
# fed to the sandbox_agent prompt + agent_command templates.
# ---------------------------------------------------------------------------


async def _run_sandbox_agent(node_def: dict[str, Any], state: dict[str, Any]) -> tuple[dict[str, Any], MagicMock]:
    """Run a sandbox_agent node with a mocked E2B sandbox.

    Returns ``(result, sandbox)`` — ``sandbox.commands.run.call_args.args[0]``
    is the wrapped command actually dispatched when the run completes.
    """
    from modulo.core.pipeline_engine.node_runner import make_sandbox_agent_fn

    fn = make_sandbox_agent_fn(node_def)

    cmd_result = MagicMock()
    cmd_result.exit_code = 0
    cmd_result.stdout = "agent stdout"
    cmd_result.stderr = ""

    handle = MagicMock()
    handle.wait = AsyncMock(return_value=cmd_result)

    sandbox = MagicMock()
    sandbox.files.write = AsyncMock()
    sandbox.files.read = AsyncMock(return_value='{"summary": "done"}')
    sandbox.files.get_info = AsyncMock(return_value=MagicMock(size=0))
    sandbox.commands.run = AsyncMock(return_value=handle)
    sandbox.kill = AsyncMock()

    with patch("e2b.AsyncSandbox.create", new=AsyncMock(return_value=sandbox)):
        result = await fn(state)
    return result, sandbox


@pytest.mark.asyncio
async def test_sandbox_agent_context_scope_allows_in_scope_key() -> None:
    """FAR-436: an in-scope ``run_context`` key renders in the agent_command.

    ``context_scope=["input"]`` keeps only the ``input`` key (plus internal
    control keys). The command references an in-scope key so it renders and the
    node completes with the scoped value.
    """
    node_def = {
        "id": "sbx-a",
        "agent_prompt": "Do the thing",
        "agent_command": "echo {{ run_context.input.task }}",
        "capability_scope": {"context_scope": ["input"]},
    }
    state = {
        "run_context": {"input": {"task": "scoped-task"}, "secret_tokens": "s3cr3t"},
        "_run_id": "run-1",
        "_pipeline_id": "pipe-1",
        "_org_id": "org-1",
    }
    result, sandbox = await _run_sandbox_agent(node_def, state)
    assert result["output"]["status"] == "completed"
    assert "echo scoped-task" in sandbox.commands.run.call_args.args[0]
    assert "s3cr3t" not in sandbox.commands.run.call_args.args[0]


@pytest.mark.asyncio
async def test_sandbox_agent_context_scope_filters_out_of_scope_key() -> None:
    """FAR-436: an out-of-scope ``run_context`` key is removed from the view.

    ``context_scope=["input"]`` strips ``secret_tokens``. A command that
    CHAIN-navigates the missing key (``{{ run_context.secret_tokens.value }}``)
    renders it undefined, so the node is safely skipped rather than dispatching
    with the secret in-scope.
    """
    node_def = {
        "id": "sbx-b",
        "agent_prompt": "Do the thing",
        "agent_command": "echo {{ run_context.secret_tokens.value }}",
        "capability_scope": {"context_scope": ["input"]},
    }
    state = {
        "run_context": {"input": {"task": "scoped-task"}, "secret_tokens": "s3cr3t"},
        "_run_id": "run-1",
        "_pipeline_id": "pipe-1",
        "_org_id": "org-1",
    }
    result, _ = await _run_sandbox_agent(node_def, state)
    assert result["status"] == "skipped"
    assert "agent_command template references missing input fields" in result["summary"]


@pytest.mark.asyncio
async def test_sandbox_agent_absent_context_scope_preserves_legacy() -> None:
    """FAR-436: absent ``capability_scope`` feeds the FULL ``run_context`` view.

    No context_scope -> no narrowing: an out-of-scope-looking key is still
    visible exactly as before the scope feature existed.
    """
    node_def = {
        "id": "sbx-c",
        "agent_prompt": "Do the thing",
        "agent_command": "echo {{ run_context.secret_tokens }}",
    }
    state = {
        "run_context": {"input": {"task": "scoped-task"}, "secret_tokens": "s3cr3t"},
        "_run_id": "run-1",
        "_pipeline_id": "pipe-1",
        "_org_id": "org-1",
    }
    result, sandbox = await _run_sandbox_agent(node_def, state)
    assert result["output"]["status"] == "completed"
    assert "echo s3cr3t" in sandbox.commands.run.call_args.args[0]
