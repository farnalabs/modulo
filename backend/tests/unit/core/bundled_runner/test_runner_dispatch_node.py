"""Unit tests for the Bundled Runner node execution adapter (FAR-590 D4).

Covers :func:`run_bundled_runner_node` (script + llm modes, idempotency
gate skip, dispatch-marker denial, wall-clock budget overrun, output parse
failures, schema-validation failure, delivery-sentinel retention, empty
command guard) and the streaming/file IO helpers
(:func:`_write_file_via_exec`, :func:`_read_file_via_exec`,
 :func:`_publish_stream_chunk`, :func:`_consume_stream`,
:func:`_resolve_stall_timeout`, :func:`_combine_raw_outputs`,
:func:`_source_contains_sentinel`), plus the FAR-792 per-node stdout/stderr
retention cap (:func:`_resolve_stdout_cap`), and the FAR-811
stdout_artifact pointer persistence (over-cap writes to artifact store,
under-cap stays inline).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modulo.core.bundled_runner import runner_dispatch
from modulo.core.bundled_runner.runner_dispatch import (
    RunnerDispatchRoute,
    _combine_raw_outputs,
    _consume_stream,
    _no_output_message,
    _publish_stream_chunk,
    _read_file_via_exec,
    _resolve_stall_timeout,
    _run_broker_for,
    _source_contains_sentinel,
    _write_file_via_exec,
)
from modulo.core.pipeline_engine.node_runner import (
    SandboxNodeFailedError as _RealSandboxNodeFailedError,
)
from modulo.core.pipeline_engine.node_runner import (
    _redact_raw_output as _real_redact_raw_output,
)
from modulo.core.pipeline_engine.node_runner import (
    _validate_against_schema as _real_validate_against_schema,
)


class _FakeError(Exception):
    def __init__(self, msg: str = "", *, node_id: str | None = None) -> None:
        super().__init__(msg)
        self.node_id = node_id


@dataclass
class _FakeOutput:
    status: str = "completed"
    summary: str = ""
    exit_code: int = 0
    wall_clock_time_ms: int = 0
    cost_estimate_usd: float = 0.0
    cost_source: object = None
    output_json: object = None
    agent_stdout: str = ""
    agent_stderr: str = ""
    stdout_length: int = 0
    stderr_length: int = 0
    stdout_truncated: bool = False
    stdout_artifact: object = None
    stderr_artifact: object = None
    attempt_key: str | None = None
    agent_status: object = None
    agent_outcome: object = None
    changed_files: object = None
    pr_url: object = None
    sandbox_session_lost: bool = False
    modulo_synthetic_failure: bool = False


def _patch_node_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every ``modulo.core.pipeline_engine.node_runner`` symbol that
    ``runner_dispatch`` imports with a deterministic test double."""
    import modulo.core.pipeline_engine.node_runner as nrm

    stubs: dict[str, object] = {
        "_MAX_ARTIFACT_LOG": 2000,
        "_FULL_MODE_DEFAULT_MAX_BYTES": 5_242_880,
        "SandboxNodeFailedError": _FakeError,
        "ScriptBudgetKilledError": _FakeError,
        "ScriptFailedError": _FakeError,
        "ScriptInvalidOutputError": _FakeError,
        "ScriptSideEffectUnknownError": _FakeError,
        "SupersededNodeError": _FakeError,
        "_SandboxNodeOutput": _FakeOutput,
        "_build_sandbox_envs": lambda **kw: {"MODULO_RUN_ID": kw.get("run_id", "")},
        "_build_sandbox_node_envelope": lambda **kw: {"envelope": True, **kw},
        "_compute_sandbox_cost": lambda *a, **k: 0.0,
        "_configure_stall_detector": lambda **kw: SimpleNamespace(touch=lambda *a, **k: None),
        "_emit_script_span_event": lambda *a, **k: None,
        "_idempotency_gate_skipped_envelope": lambda node_id: {"status": "skipped", "node_id": node_id},
        "_is_sandbox_session_lost_echo": lambda out: False,
        "_marker_delivery_done_for_node": lambda *a, **k: False,
        "_persist_full_stderr_artifact": lambda **kw: None,
        "_persist_full_stdout_artifact": lambda **kw: None,
        "_read_run_raw_output_markers_for_gate": AsyncMock(return_value=[]),
        "_read_org_stdout_retention_ceiling": AsyncMock(return_value=None),
        "_redact_raw_output": lambda s: s,
        "_retain_raw_output_marker": AsyncMock(),
        "_run_identity_strs": lambda state: (
            state.get("_run_id", "run-1"),
            state.get("_pipeline_id", "pipe-1"),
            state.get("_org_id", str(uuid.uuid4())),
        ),
        "_sandbox_acquire_dispatch_marker": AsyncMock(return_value="attempt-key"),
        "_sandbox_clear_dispatch_marker": AsyncMock(),
        "_sandbox_mint_run_api_key_for_sandbox": AsyncMock(return_value="apikey"),
        "_sandbox_resolve_secret_ref": AsyncMock(return_value="secret"),
        "_sandbox_store_dispatch_marker_sandbox": AsyncMock(),
        "_sandbox_store_script_lease": AsyncMock(),
        "_sandbox_wallclock_budget_exceeded": lambda **kw: False,
        "_validate_against_schema": lambda *a, **k: None,
        "resolve_env_var_refs": AsyncMock(return_value={}),
        "_normalize_marker_text": lambda s: s or "",
        "_source_contains_delivery_sentinel": lambda text, sentinel: bool(text and sentinel and sentinel in text),
        "_SANDBOX_IDLE_TIMEOUT": 30,
    }
    for name, val in stubs.items():
        monkeypatch.setattr(nrm, name, val, raising=False)


@pytest.fixture
def patch_node_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_node_runner(monkeypatch)
    import modulo.core.capability_scope as cs

    monkeypatch.setattr(cs, "filter_run_context_scope", lambda rc, scope: rc, raising=False)
    import modulo.core.pipeline_engine.event_broker as eb

    monkeypatch.setattr(eb, "get_registry", lambda: SimpleNamespace(get=lambda *a, **k: None), raising=False)
    import modulo.settings as st

    monkeypatch.setattr(
        st,
        "get_settings",
        lambda: SimpleNamespace(modulo_idempotency_gate_enabled=True, modulo_max_local_concurrency=2),
        raising=False,
    )


@dataclass
class _ExecResult:
    exit_code: int = 0
    stdout: str = '{"summary":"ok"}'
    stderr: str = ""


class _FakeExecProcess:
    def __init__(self, chunks, *, exit_code=0, error=None):
        self._chunks = chunks
        self.exit_code = exit_code
        self.error = error
        self.done = asyncio.Event()
        self._killed = False

    async def _gen(self):
        for stream, data in self._chunks:
            yield SimpleNamespace(stream=stream, data=data)
        self.done.set()

    @property
    def chunks(self):
        return self._gen()

    async def kill(self):
        self._killed = True


class _FakeProvider:
    def __init__(self, *, exec_result=None, stream_chunks=None, stream_exit=0, stream_error=None):
        self._exec_result = exec_result or _ExecResult()
        self._stream_chunks = stream_chunks or []
        self._stream_exit = stream_exit
        self._stream_error = stream_error
        self.created = []
        self.destroyed = []
        self.closed = False
        self._workspaces = {"ws-1": "container-1"}

    async def create_workspace(self, spec):
        self.created.append(spec)
        return "ws-1"

    async def exec_command(self, ref, cmd, *, cmd_timeout=None):
        return self._exec_result

    async def exec_command_stream(self, ref, cmd, *, environment=None):
        return _FakeExecProcess(self._stream_chunks, exit_code=self._stream_exit, error=self._stream_error)

    async def destroy_workspace(self, ref):
        self.destroyed.append(ref)

    async def close(self):
        self.closed = True


def _config(**overrides) -> SimpleNamespace:
    base = {
        "node_id": "node-1",
        "node_def": {"capability_scope": {}},
        "sandbox_mode": "script",
        "agent_command": "echo hi",
        "agent_prompt_template": "",
        "wallclock_budget_seconds": None,
        "output_schema_json": None,
        "sandbox_timeout": 60,
        "stall_timeout_override": None,
        "context_files": {},
        "delivery_sentinel": None,
        "session_factory": lambda: _session_cm(),
        "single_sandbox_node": False,
        "loop_intercept_config": None,
        "enable_heartbeat": True,
        "stdout_percentage_delta": 0.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _session_cm():
    return SimpleNamespace(
        __aenter__=AsyncMock(return_value=SimpleNamespace()),
        __aexit__=AsyncMock(return_value=False),
    )


def _route(provider=None):
    return RunnerDispatchRoute(
        provider_type="runner_docker",
        profile=SimpleNamespace(
            id=uuid.uuid4(),
            provider_type="runner_docker",
            image_ref="modulo-runner:opencode@sha256:" + "0" * 64,
            capabilities_json=[],
            config_json={"timeout_seconds": 1800, "memory_mb": 1024, "workspace_network": "modulo-runner-workspace"},
            network_policy="outbound",
            persistence_policy="ephemeral",
            organisation_id=uuid.uuid4(),
        ),
        provider=provider or _FakeProvider(),
        hub=SimpleNamespace(aclose=AsyncMock()),
    )


def _state():
    return {
        "_run_id": "run-1",
        "_pipeline_id": "pipe-1",
        "_org_id": str(uuid.uuid4()),
        "run_context": {"input": {"q": "hello"}},
        "_claim_lease": None,
    }


async def test_run_script_mode_happy_path(patch_node_runner) -> None:
    provider = _FakeProvider()
    out = await runner_dispatch.run_bundled_runner_node(_state(), _config(), _route(provider))
    assert out["envelope"] is True
    assert provider.created  # workspace was provisioned


async def test_run_llm_mode_happy_path(patch_node_runner) -> None:
    cfg = _config(sandbox_mode="llm", agent_prompt_template="Hello {{ input.q }}")
    out = await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route())
    assert out["envelope"] is True


async def test_run_llm_mode_writes_prompt(patch_node_runner, monkeypatch) -> None:
    written = {}

    async def _fake_write(provider, ref, path, content):
        written[path] = content

    monkeypatch.setattr(runner_dispatch, "_write_file_via_exec", _fake_write)
    cfg = _config(sandbox_mode="llm", agent_prompt_template="Prompt: {{ input.q }}")
    await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route())
    assert written.get("/home/user/prompt.md") == "Prompt: hello"


async def test_run_template_missing_input_skips(patch_node_runner) -> None:
    cfg = _config(sandbox_mode="llm", agent_prompt_template="Hello {{ missing.field }}")
    out = await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route())
    assert out["status"] == "skipped"
    assert "missing input" in out["summary"]


async def test_run_empty_rendered_command_raises(patch_node_runner, monkeypatch) -> None:
    monkeypatch.setattr(runner_dispatch, "_write_file_via_exec", AsyncMock())
    cfg = _config(sandbox_mode="llm", agent_command="   ")
    with pytest.raises(ValueError, match="empty command"):
        await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route())


async def test_run_dispatch_marker_denied_raises(patch_node_runner, monkeypatch) -> None:
    import modulo.core.pipeline_engine.node_runner as nrm

    monkeypatch.setattr(nrm, "_sandbox_acquire_dispatch_marker", AsyncMock(return_value=None), raising=False)
    with pytest.raises(_FakeError):
        await runner_dispatch.run_bundled_runner_node(_state(), _config(), _route())


async def test_run_route_without_profile_provider_raises(patch_node_runner) -> None:
    route = RunnerDispatchRoute(provider_type="runner_docker", profile=None, provider=None, hub=None)
    with pytest.raises(RuntimeError):
        await runner_dispatch.run_bundled_runner_node(_state(), _config(), route)


async def test_run_idempotency_gate_skip(patch_node_runner, monkeypatch) -> None:
    import modulo.core.pipeline_engine.node_runner as nrm

    monkeypatch.setattr(nrm, "_marker_delivery_done_for_node", lambda *a, **k: True, raising=False)
    cfg = _config(single_sandbox_node=True, delivery_sentinel="DONE")
    out = await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route())
    assert out["status"] == "skipped"


async def test_run_wallclock_budget_overrun_script(patch_node_runner, monkeypatch) -> None:
    import modulo.core.pipeline_engine.node_runner as nrm

    monkeypatch.setattr(nrm, "_sandbox_wallclock_budget_exceeded", lambda **kw: True, raising=False)
    with pytest.raises(_FakeError):
        await runner_dispatch.run_bundled_runner_node(_state(), _config(), _route())


async def test_run_output_parse_failure_script_raises(patch_node_runner, monkeypatch) -> None:
    provider = _FakeProvider(exec_result=_ExecResult(exit_code=0, stdout="not-json"))
    monkeypatch.setattr(runner_dispatch, "_read_file_via_exec", AsyncMock(return_value="not valid json {"))
    with pytest.raises(_FakeError):
        await runner_dispatch.run_bundled_runner_node(_state(), _config(), _route(provider))


async def test_run_output_parse_failure_llm_raises(patch_node_runner, monkeypatch) -> None:
    monkeypatch.setattr(runner_dispatch, "_read_file_via_exec", AsyncMock(return_value="not json {"))
    cfg = _config(sandbox_mode="llm")
    provider = _FakeProvider(exec_result=_ExecResult(exit_code=0))
    with pytest.raises(_FakeError):
        await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(provider))


async def test_run_schema_validation_failure_script_raises(patch_node_runner, monkeypatch) -> None:
    import modulo.core.pipeline_engine.node_runner as nrm

    def _bad_schema(*a, **k):
        raise ValueError("schema mismatch")

    monkeypatch.setattr(nrm, "_validate_against_schema", _bad_schema, raising=False)
    monkeypatch.setattr(runner_dispatch, "_read_file_via_exec", AsyncMock(return_value='{"summary":"ok"}'))
    cfg = _config(output_schema_json={"type": "object"})
    with pytest.raises(_FakeError):
        await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(_FakeProvider()))


async def test_run_schema_validation_failure_llm_raises_retryable(patch_node_runner, monkeypatch) -> None:
    import modulo.core.pipeline_engine.node_runner as nrm

    def _bad_schema(*a, **k):
        raise ValueError("schema mismatch")

    retained = {}

    async def _retain(*a, **k):
        retained.update(k)

    monkeypatch.setattr(nrm, "_validate_against_schema", _bad_schema, raising=False)
    monkeypatch.setattr(nrm, "_retain_raw_output_marker", _retain, raising=False)
    monkeypatch.setattr(runner_dispatch, "_read_file_via_exec", AsyncMock(return_value='{"summary":"ok"}'))
    cfg = _config(sandbox_mode="llm", output_schema_json={"type": "object", "required": ["pr_url"]})
    with pytest.raises(_FakeError) as excinfo:
        await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(_FakeProvider()))
    assert "schema validation" in str(excinfo.value)
    assert retained.get("parse_error") == "schema mismatch"
    assert '{"summary":"ok"}' in retained.get("source", "")


async def test_run_schema_validation_failure_llm_retryable_real_schema(patch_node_runner, monkeypatch) -> None:
    """FAR-780 regression: an llm-mode output that violates the node's DECLARED
    output schema (a required field such as ``pr_url`` missing) raises the
    retryable :class:`SandboxNodeFailedError` — ``runtime_retry`` re-dispatches
    the node in a fresh sandbox — instead of returning the synthetic ``status=
    failed`` envelope that completed the node non-retryably and eval-blocked the
    run (``EvalBlockedError`` is a never-retryable control flow fault)."""
    import modulo.core.pipeline_engine.node_runner as nrm
    from modulo.core.pipeline_engine import runtime_retry

    monkeypatch.setattr(nrm, "SandboxNodeFailedError", _RealSandboxNodeFailedError, raising=False)
    monkeypatch.setattr(nrm, "_validate_against_schema", _real_validate_against_schema, raising=False)
    monkeypatch.setattr(
        runner_dispatch,
        "_read_file_via_exec",
        AsyncMock(return_value='{"summary": "done"}'),
    )
    cfg = _config(
        sandbox_mode="llm",
        output_schema_json={"type": "object", "required": ["pr_url"]},
        schema_validator_mode="strict",
    )
    with pytest.raises(_RealSandboxNodeFailedError) as excinfo:
        await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(_FakeProvider()))
    assert "required" in str(excinfo.value)
    assert runtime_retry.failure_event(excinfo.value) == "error"


async def test_run_script_mode_nonzero_exit_raises(patch_node_runner, monkeypatch) -> None:
    monkeypatch.setattr(runner_dispatch, "_read_file_via_exec", AsyncMock(return_value='{"summary":"ok"}'))
    with pytest.raises(_FakeError):
        await runner_dispatch.run_bundled_runner_node(_state(), _config(), _route(_FakeProvider(stream_exit=3)))


async def test_run_delivery_sentinel_retained(patch_node_runner, monkeypatch) -> None:
    import modulo.core.pipeline_engine.node_runner as nrm

    retained = {}

    async def _retain(*a, **k):
        retained.update(k)

    monkeypatch.setattr(nrm, "_retain_raw_output_marker", _retain, raising=False)
    monkeypatch.setattr(runner_dispatch, "_read_file_via_exec", AsyncMock(return_value='{"summary":"done"}'))
    await runner_dispatch.run_bundled_runner_node(
        _state(),
        _config(sandbox_mode="llm", delivery_sentinel="DONE"),
        _route(_FakeProvider(stream_chunks=[("stdout", "DONE observed")], stream_exit=0)),
    )
    assert retained.get("delivery_sentinel") == "DONE"


async def test_run_stream_error_raises_retryable(patch_node_runner, monkeypatch) -> None:
    monkeypatch.setattr(runner_dispatch, "_read_file_via_exec", AsyncMock(return_value=""))
    cfg = _config()
    provider = _FakeProvider(stream_chunks=[("stdout", "x")], stream_error="engine drop")
    with pytest.raises(_FakeError):
        await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(provider))


async def test_run_llm_mode_surfaces_agent_status_and_outcome(patch_node_runner, monkeypatch) -> None:
    monkeypatch.setattr(
        runner_dispatch,
        "_read_file_via_exec",
        AsyncMock(return_value='{"summary":"boom","status":"failed","outcome":"error"}'),
    )
    cfg = _config(sandbox_mode="llm")
    out = await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(_FakeProvider()))
    # A1 elevation input: the agent's RAW verdict is surfaced verbatim (FAR-188
    # parity with the E2B path) so the executor's _node_output_agent_failure can
    # fire — a self-reported failure must NOT be silently swallowed as complete.
    assert out["output"].agent_status == "failed"
    assert out["output"].agent_outcome == "error"
    # Node-level status still tracks exit_code (the executor elevates the run).
    assert out["output"].status == "completed"


async def test_run_llm_mode_surfaces_changed_files_and_pr_url(patch_node_runner, monkeypatch) -> None:
    monkeypatch.setattr(
        runner_dispatch,
        "_read_file_via_exec",
        AsyncMock(return_value='{"summary":"done","changed_files":["a.py"],"pr_url":"https://x"}'),
    )
    cfg = _config(sandbox_mode="llm")
    out = await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(_FakeProvider()))
    assert out["output"].changed_files == ["a.py"]
    assert out["output"].pr_url == "https://x"


async def test_run_llm_mode_non_dict_output_continues(patch_node_runner, monkeypatch) -> None:
    # FAR-188 parity: a parseable-but-non-dict output.json (a list here) retains
    # the raw evidence marker and CONTINUES (agent_status stays None) rather than
    # being misclassified as a no-output retryable SandboxNodeFailedError.
    monkeypatch.setattr(runner_dispatch, "_read_file_via_exec", AsyncMock(return_value="[1, 2, 3]"))
    cfg = _config(sandbox_mode="llm")
    out = await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(_FakeProvider()))
    assert out["output"].agent_status is None
    assert out["output"].status == "completed"


async def test_run_llm_mode_missing_agent_status_not_failed(patch_node_runner, monkeypatch) -> None:
    monkeypatch.setattr(runner_dispatch, "_read_file_via_exec", AsyncMock(return_value='{"summary":"ok"}'))
    cfg = _config(sandbox_mode="llm")
    out = await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(_FakeProvider()))
    assert out["output"].agent_status is None
    assert out["output"].status == "completed"


async def test_run_llm_mode_session_lost_forces_failed(patch_node_runner, monkeypatch) -> None:
    import modulo.core.pipeline_engine.node_runner as nrm

    monkeypatch.setattr(nrm, "_is_sandbox_session_lost_echo", lambda out: True, raising=False)
    monkeypatch.setattr(runner_dispatch, "_read_file_via_exec", AsyncMock(return_value='{"summary":"ok"}'))
    cfg = _config(sandbox_mode="llm")
    out = await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(_FakeProvider()))
    assert out["output"].status == "failed"
    assert out["output"].sandbox_session_lost is True


async def test_run_context_files_written(patch_node_runner, monkeypatch) -> None:
    written = {}

    async def _fake_write(provider, ref, path, content):
        written[path] = content

    monkeypatch.setattr(runner_dispatch, "_write_file_via_exec", _fake_write)
    cfg = _config(context_files={"notes.txt.b64": base64.b64encode(b"hi").decode()})
    await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route())
    assert written.get("notes.txt") == "hi"


# ---------------------------------------------------------------------------
# FAR-792: per-node stdout/stderr retention — tail (512KB) vs full
# (stdout_max_bytes) on the Bundled Runner path.
# ---------------------------------------------------------------------------


async def test_run_stdout_tail_mode_caps_at_legacy_limit(patch_node_runner) -> None:
    """stdout_retention_mode="tail" (the default) keeps the legacy artifact cap
    (the patched ``_MAX_ARTIFACT_LOG`` here) EVEN when stdout_max_bytes is
    offered — full retention must be opted into."""
    cfg = _config(node_def={"capability_scope": {}, "stdout_retention_mode": "tail", "stdout_max_bytes": 999})
    cap = runner_dispatch._resolve_stdout_cap(cfg.node_def)
    long_stdout = "x" * 4096
    provider = _FakeProvider(stream_chunks=[("stdout", long_stdout)], stream_exit=0)
    out = await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(provider))
    assert out["output"].agent_stdout == "x" * cap
    assert out["output"].stdout_length == len(long_stdout)
    assert out["output"].stdout_truncated is True


async def test_run_stdout_full_retention_honors_stdout_max_bytes(patch_node_runner) -> None:
    """stdout_retention_mode="full" + stdout_max_bytes keeps up to the configured
    cap (not the fixed 512KB) and surfaces stdout_truncated + the full length."""
    cfg = _config(node_def={"capability_scope": {}, "stdout_retention_mode": "full", "stdout_max_bytes": 2048})
    long_stdout = "x" * 4096
    provider = _FakeProvider(stream_chunks=[("stdout", long_stdout)], stream_exit=0)
    out = await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(provider))
    assert out["output"].agent_stdout == "x" * 2048
    assert out["output"].stdout_length == len(long_stdout)
    assert out["output"].stdout_truncated is True


async def test_run_full_retention_caps_raw_output_marker(patch_node_runner, monkeypatch) -> None:
    """FAR-792: in full mode the raw-output retention marker honours the
    node's effective cap too (E2B parity), not just the envelope artifacts."""
    import modulo.core.pipeline_engine.node_runner as nrm

    retained = {}

    async def _retain(*a, **k):
        retained.update(k)

    monkeypatch.setattr(nrm, "_retain_raw_output_marker", _retain, raising=False)
    monkeypatch.setattr(runner_dispatch, "_read_file_via_exec", AsyncMock(return_value="not json {"))
    cfg = _config(node_def={"capability_scope": {}, "stdout_retention_mode": "full", "stdout_max_bytes": 2048})
    with pytest.raises(_FakeError):
        await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(_FakeProvider()))
    assert retained.get("max_artifact_bytes") == 2048


async def test_run_stdout_org_ceiling_clamps_cap(patch_node_runner, monkeypatch) -> None:
    """FAR-811: the org-level hard ceiling (system_config.sandbox_stdout_retention_max_bytes)
    is read and applied on the Bundled Runner path so the "no node can exceed the org
    ceiling" invariant holds here exactly as on the E2B path. A node requesting full
    retention at 2048 bytes is hard-clamped to the 1024-byte org ceiling."""
    import modulo.core.pipeline_engine.node_runner as nrm

    monkeypatch.setattr(
        nrm,
        "_read_org_stdout_retention_ceiling",
        AsyncMock(return_value=1024),
        raising=False,
    )
    cfg = _config(node_def={"capability_scope": {}, "stdout_retention_mode": "full", "stdout_max_bytes": 2048})
    long_stdout = "x" * 4096
    provider = _FakeProvider(stream_chunks=[("stdout", long_stdout)], stream_exit=0)
    out = await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(provider))
    assert out["output"].agent_stdout == "x" * 1024
    assert out["output"].stdout_length == len(long_stdout)
    assert out["output"].stdout_truncated is True


def test_resolve_stdout_cap_applies_org_ceiling(patch_node_runner) -> None:
    """The pure helper passes the org ceiling through to the shared resolver."""
    assert (
        runner_dispatch._resolve_stdout_cap(
            {"stdout_retention_mode": "full", "stdout_max_bytes": 2048},
            org_ceiling=1024,
        )
        == 1024
    )
    # No ceiling -> node's own cap applies unchanged.
    assert runner_dispatch._resolve_stdout_cap({"stdout_retention_mode": "full", "stdout_max_bytes": 2048}) == 2048


def test_resolve_stdout_cap_node_max_bytes_only_wins(patch_node_runner) -> None:
    """FAR-811: a node that sets only stdout_max_bytes (no mode) keeps its value.

    The explicit max_bytes must not be discarded for the pipeline default's
    max_bytes; the mode is inherited from the pipeline default.
    """
    assert (
        runner_dispatch._resolve_stdout_cap(
            {"stdout_max_bytes": 2048},
            pipeline_default={"mode": "full", "max_bytes": 4096},
        )
        == 2048
    )


async def test_run_stdout_full_retention_default_holds_whole_stream(patch_node_runner) -> None:
    """stdout_retention_mode="full" without stdout_max_bytes retains the whole
    stream (default 5MB cap) — no truncation flag for an under-cap stream."""
    cfg = _config(node_def={"capability_scope": {}, "stdout_retention_mode": "full"})
    long_stdout = "x" * 4096
    provider = _FakeProvider(stream_chunks=[("stdout", long_stdout)], stream_exit=0)
    out = await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(provider))
    assert out["output"].agent_stdout == long_stdout
    assert out["output"].stdout_length == len(long_stdout)
    assert out["output"].stdout_truncated is False


async def test_run_stdout_redacts_before_truncation(patch_node_runner, monkeypatch) -> None:
    """Redaction runs on the FULL stream before the cap slice, so a tokenized
    URL whose terminating ``@`` would be cut by the cap is still masked in the
    retained artifact (FAR-792 redact-before-truncate ordering)."""
    import modulo.core.pipeline_engine.node_runner as nrm

    monkeypatch.setattr(nrm, "_redact_raw_output", _real_redact_raw_output, raising=False)
    cfg = _config(node_def={"capability_scope": {}, "stdout_retention_mode": "full", "stdout_max_bytes": 100})
    # The ``@`` terminator of the tokenized URL sits at offset 100+ — beyond a
    # 100-byte cap. Truncate-then-redact keeps the raw `https://git:BBBB...`
    # prefix (no @ in the window, so the URL pattern cannot match it).
    secret_url = "https://git:" + ("B" * 40) + "@github.com/org/repo"
    provider = _FakeProvider(stream_chunks=[("stdout", "u" * 80 + secret_url)], stream_exit=0)
    out = await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(provider))
    assert "<redacted>" in out["output"].agent_stdout
    assert "BBB" not in out["output"].agent_stdout
    assert out["output"].stdout_truncated is True


async def test_run_stdout_truncation_warns(patch_node_runner, caplog) -> None:
    """Truncation emits a warning log carrying the node id and the effective cap."""
    import logging

    cfg = _config(node_def={"capability_scope": {}, "stdout_retention_mode": "full", "stdout_max_bytes": 128})
    provider = _FakeProvider(stream_chunks=[("stdout", "x" * 4096)], stream_exit=0)
    with caplog.at_level(logging.WARNING):
        out = await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(provider))
    assert out["output"].stdout_truncated is True
    assert "sandbox_agent.stdout_stderr_truncated" in caplog.text
    record = next(r for r in caplog.records if r.getMessage() == "sandbox_agent.stdout_stderr_truncated")
    assert record.__dict__.get("node_id") == "node-1"
    assert record.__dict__.get("retention_cap_bytes") == 128


async def test_write_file_via_exec_success(patch_node_runner) -> None:
    calls = {}

    class _P:
        async def exec_command(self, ref, cmd, *, cmd_timeout=None):
            calls["cmd"] = cmd
            return _ExecResult(exit_code=0)

    await _write_file_via_exec(_P(), "ws", "/home/user/a.txt", "data")
    assert "base64" in calls["cmd"][2]


async def test_write_file_via_exec_failure_raises(patch_node_runner) -> None:
    class _P:
        async def exec_command(self, ref, cmd, *, cmd_timeout=None):
            return _ExecResult(exit_code=1, stderr="boom")

    with pytest.raises(RuntimeError):
        await _write_file_via_exec(_P(), "ws", "/home/user/a.txt", "data")


async def test_read_file_via_exec_success(patch_node_runner) -> None:
    class _P:
        async def exec_command(self, ref, cmd, *, cmd_timeout=None):
            return _ExecResult(exit_code=0, stdout="payload")

    assert await _read_file_via_exec(_P(), "ws", "/home/user/a.txt") == "payload"


async def test_read_file_via_exec_missing_returns_empty(patch_node_runner) -> None:
    class _P:
        async def exec_command(self, ref, cmd, *, cmd_timeout=None):
            return _ExecResult(exit_code=0, stdout="")

    assert not await _read_file_via_exec(_P(), "ws", "/home/user/a.txt")


def test_publish_stream_chunk_no_broker(patch_node_runner) -> None:
    assert _publish_stream_chunk(None, node_id="n", chunk="", stream="stdout", throttle_state={}) is None


def test_publish_stream_chunk_buffers_within_window(patch_node_runner) -> None:
    import time as _time

    published = []
    broker = SimpleNamespace(publish=lambda topic, payload: published.append((topic, payload)))
    now = _time.monotonic()
    state = {"buf": [], "last_ts": now}
    _publish_stream_chunk(broker, node_id="n", chunk="a", stream="stdout", throttle_state=state)
    _publish_stream_chunk(broker, node_id="n", chunk="b", stream="stdout", throttle_state=state)
    assert published == []


def test_publish_stream_chunk_flushes_after_interval(patch_node_runner, monkeypatch) -> None:
    published = []
    broker = SimpleNamespace(publish=lambda topic, payload: published.append((topic, payload)))

    class _FT:
        t = 0.0

        @classmethod
        def monotonic(cls):
            return cls.t

    monkeypatch.setattr(runner_dispatch.time, "monotonic", _FT.monotonic)
    state = {"buf": [], "last_ts": 0.0}
    _publish_stream_chunk(broker, node_id="n", chunk="a", stream="stdout", throttle_state=state)
    _FT.t = 5.0
    _publish_stream_chunk(broker, node_id="n", chunk="b", stream="stdout", throttle_state=state)
    assert len(published) == 1
    assert published[0][1]["chunk"] == "ab"


def test_publish_stream_chunk_broker_error_swallowed(patch_node_runner) -> None:
    broker = SimpleNamespace(publish=lambda topic, payload: (_ for _ in ()).throw(RuntimeError("closed")))
    state = {"buf": ["x"], "last_ts": 0.0}
    assert _publish_stream_chunk(broker, node_id="n", chunk="x", stream="stdout", throttle_state=state) is None


async def test_consume_stream_success(patch_node_runner) -> None:
    proc = _FakeExecProcess([("stdout", "a"), ("stderr", "b")], exit_code=0)
    collected, timed_out, stalled = await _consume_stream(proc, node_id="n", sandbox_timeout=10, stall_timeout=5)
    assert ("stdout", "a") in collected
    assert ("stderr", "b") in collected
    assert not timed_out and not stalled


async def test_consume_stream_timeout_kills(patch_node_runner) -> None:
    proc = _FakeExecProcess([], exit_code=None)

    async def _slow():
        await asyncio.sleep(2)
        yield SimpleNamespace(stream="stdout", data="late")

    proc._gen = _slow
    _, timed_out, _stalled = await _consume_stream(proc, node_id="n", sandbox_timeout=0.05, stall_timeout=5)
    assert timed_out is True
    assert proc._killed is True


async def test_consume_stream_stall_kills(patch_node_runner) -> None:
    proc = _FakeExecProcess([], exit_code=None)

    async def _hang():
        await asyncio.sleep(10)
        yield SimpleNamespace(stream="stdout", data="late")

    proc._gen = _hang
    _, _, _stalled = await _consume_stream(proc, node_id="n", sandbox_timeout=30, stall_timeout=0.05)
    assert _stalled is True
    assert proc._killed is True


async def test_consume_stream_error_propagates(patch_node_runner) -> None:
    proc = _FakeExecProcess([], exit_code=None, error="engine drop")
    _, timed_out, stalled = await _consume_stream(proc, node_id="n", sandbox_timeout=30, stall_timeout=30)
    assert not timed_out and not stalled


def test_resolve_stall_timeout_default(patch_node_runner) -> None:
    assert _resolve_stall_timeout(None) == 30.0


def test_resolve_stall_timeout_invalid_falls_back(patch_node_runner, monkeypatch) -> None:
    import modulo.core.pipeline_engine.node_runner as nrm

    monkeypatch.setattr(nrm, "_SANDBOX_IDLE_TIMEOUT", 30, raising=False)
    assert _resolve_stall_timeout("not-a-number") == 30.0


def test_resolve_stall_timeout_explicit(patch_node_runner) -> None:
    assert _resolve_stall_timeout(12.5) == 12.5


def test_resolve_stdout_cap_coercion(patch_node_runner) -> None:
    """FAR-792: the effective cap falls back to the safe legacy defaults for
    malformed / missing per-node retention config."""
    import modulo.core.pipeline_engine.node_runner as nrm

    assert runner_dispatch._resolve_stdout_cap({}) == nrm._MAX_ARTIFACT_LOG
    assert runner_dispatch._resolve_stdout_cap({"stdout_retention_mode": "full"}) == nrm._FULL_MODE_DEFAULT_MAX_BYTES
    assert (
        runner_dispatch._resolve_stdout_cap({"stdout_retention_mode": "all", "stdout_max_bytes": 99})
        == nrm._MAX_ARTIFACT_LOG
    )
    assert (
        runner_dispatch._resolve_stdout_cap({"stdout_retention_mode": "full", "stdout_max_bytes": -3})
        == nrm._FULL_MODE_DEFAULT_MAX_BYTES
    )
    assert (
        runner_dispatch._resolve_stdout_cap({"stdout_retention_mode": "full", "stdout_max_bytes": True})
        == nrm._FULL_MODE_DEFAULT_MAX_BYTES
    )


def test_resolve_stdout_cap_pipeline_default(patch_node_runner) -> None:
    """FAR-811: pipeline default is applied when node didn't set mode."""
    import modulo.core.pipeline_engine.node_runner as nrm

    # Node didn't set mode -> pipeline default mode=full with max_bytes
    cap = runner_dispatch._resolve_stdout_cap(
        {},
        pipeline_default={"mode": "full", "max_bytes": 4096},
    )
    assert cap == 4096

    # Node didn't set mode -> pipeline default mode=tail
    cap = runner_dispatch._resolve_stdout_cap(
        {},
        pipeline_default={"mode": "tail"},
    )
    assert cap == nrm._MAX_ARTIFACT_LOG

    # Node explicitly set mode -> pipeline default ignored
    cap = runner_dispatch._resolve_stdout_cap(
        {"stdout_retention_mode": "full", "stdout_max_bytes": 2048},
        pipeline_default={"mode": "tail"},
    )
    assert cap == 2048

    # Pipeline default clamped by org ceiling
    cap = runner_dispatch._resolve_stdout_cap(
        {},
        pipeline_default={"mode": "full", "max_bytes": 20_000_000},
        org_ceiling=5_000_000,
    )
    assert cap == 5_000_000


def test_combine_raw_outputs(patch_node_runner) -> None:
    assert _combine_raw_outputs("raw", "std") == "raw\nstd"


def test_source_contains_sentinel(patch_node_runner) -> None:
    assert _source_contains_sentinel("see DONE here", "DONE") is True
    assert _source_contains_sentinel("nothing", "DONE") is False


def test_no_output_message(patch_node_runner) -> None:
    assert "node-1" in _no_output_message("node-1")


def test_run_broker_for_empty_run_id(patch_node_runner) -> None:
    assert _run_broker_for("") is None


# ---------------------------------------------------------------------------
# Gap 3: Bundled Runner workspace-inputs fail-closed
# ---------------------------------------------------------------------------


async def test_run_bundled_runner_fails_closed_on_workspace_inputs(patch_node_runner) -> None:
    """The Bundled Runner path does NOT support managed workspace inputs.
    When inputs are configured, it must raise SandboxNodeFailedError
    rather than silently ignoring them (FAR-800 follow-up Gap 3)."""
    cfg = _config(
        workspace_inputs=[
            {
                "dest": "/home/user/repo",
                "url": "https://github.com/org/repo.git",
                "ref": {"kind": "branch", "value": "main"},
            }
        ]
    )
    # SandboxNodeFailedError is monkeypatched to _FakeError by patch_node_runner.
    with pytest.raises(_FakeError, match="workspace inputs"):
        await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route())


# ---------------------------------------------------------------------------
# FAR-811: bundled-runner stdout_artifact parity (mirrors E2B tests)
# ---------------------------------------------------------------------------


async def test_over_cap_stdout_written_to_artifact_store_with_pointer(patch_node_runner, monkeypatch) -> None:
    """Over-cap redacted stdout is retained IN FULL in the artifact store and
    the envelope carries a stdout_artifact pointer (rel_path / size_bytes /
    sha256, truncated: False, redacted: True) instead of only the truncated
    head (FAR-811 parity with the E2B path)."""
    import modulo.core.pipeline_engine.node_runner as nrm

    cap = 2048
    big_stdout = "x" * (cap + 1)
    pointer = {
        "rel_path": "org/run/node/key.stdout.zst",
        "size_bytes": cap + 1,
        "sha256": hashlib.sha256(big_stdout.encode()).hexdigest(),
        "stream": "stdout",
        "compression": "zstd",
        "truncated": False,
        "redacted": True,
    }
    monkeypatch.setattr(
        nrm,
        "_persist_full_stdout_artifact",
        lambda **kw: pointer,
        raising=False,
    )
    provider = _FakeProvider(stream_chunks=[("stdout", big_stdout)])
    cfg = _config(
        node_def={"capability_scope": {}, "stdout_retention_mode": "full", "stdout_max_bytes": cap},
    )
    out = await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(provider))
    output = out["output"]
    assert output.stdout_artifact is not None
    assert output.stdout_artifact["truncated"] is False
    assert output.stdout_artifact["redacted"] is True
    assert output.stdout_artifact["size_bytes"] == cap + 1
    assert output.stdout_artifact["sha256"] == pointer["sha256"]
    assert output.stdout_artifact["rel_path"].endswith(".zst")


async def test_under_cap_stdout_stays_inline_no_artifact(patch_node_runner, monkeypatch) -> None:
    """Under-cap stdout keeps today's inline behaviour: no stdout_artifact key
    and no artifact written to the store (FAR-811 backwards compatibility)."""
    import modulo.core.pipeline_engine.node_runner as nrm

    cap = 8192
    small_stdout = "x" * 2048
    call_log: list[str] = []

    def _spy(**kw: object) -> None:
        call_log.append("called")

    monkeypatch.setattr(nrm, "_persist_full_stdout_artifact", _spy, raising=False)
    provider = _FakeProvider(stream_chunks=[("stdout", small_stdout)])
    cfg = _config(
        node_def={"capability_scope": {}, "stdout_retention_mode": "full", "stdout_max_bytes": cap},
    )
    out = await runner_dispatch.run_bundled_runner_node(_state(), cfg, _route(provider))
    output = out["output"]
    # When under-cap, stdout_artifact stays at the _UNSET sentinel (omitted
    # from the real envelope, defaulting to None in the FakeOutput).
    assert output.stdout_artifact is nrm._UNSET
    assert not call_log  # _persist_full_stdout_artifact was never invoked
