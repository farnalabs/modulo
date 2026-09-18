"""Tests for FAR-592 / D6 runner-binding env injection in node_runner.

Covers the new lines in ``core/pipeline_engine/node_runner.py``:
``_build_sandbox_envs(..., runner_bindings=...)`` precedence and
``_runner_binding_env_profile_id()`` (the Local tier-refusal signal).
"""

import uuid

from modulo.core.pipeline_engine.node_runner import (
    _build_sandbox_envs,
    _conformance_ctx_cv,
    _runner_binding_env_profile_id,
    set_conformance_ctx,
)


def test_build_sandbox_envs_injects_runner_bindings_between_profile_and_node():
    env = _build_sandbox_envs(
        run_id="run-1",
        pipeline_id="pipe-1",
        org_id="org-1",
        input_json="{}",
        sandbox_mode="script",
        env_vars_extra={"NODE_VAR": "node", "OPENAI_API_KEY": "node-key"},
        runner_bindings={"OPENAI_API_KEY": "binding-key", "ANTHROPIC_API_KEY": "binding-anthropic"},
    )
    # System vars set exactly once.
    assert env["MODULO_RUN_ID"] == "run-1"
    assert env["MODULO_PIPELINE_ID"] == "pipe-1"
    # Node env_vars_extra wins over runner bindings (THE NODE WINS).
    assert env["OPENAI_API_KEY"] == "node-key"
    # Runner bindings present where the node did not override.
    assert env["ANTHROPIC_API_KEY"] == "binding-anthropic"
    assert env["NODE_VAR"] == "node"


def test_build_sandbox_envs_no_bindings_is_noop():
    env = _build_sandbox_envs(
        run_id="run-2",
        pipeline_id="pipe-2",
        org_id="org-2",
        input_json="{}",
        sandbox_mode="script",
        env_vars_extra={"A": "b"},
    )
    assert "OPENAI_API_KEY" not in env
    assert env["A"] == "b"


def test_build_sandbox_envs_non_script_injects_host_creds(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "host-pat")
    env = _build_sandbox_envs(
        run_id="run-3",
        pipeline_id="pipe-3",
        org_id="org-3",
        input_json="{}",
        sandbox_mode="sandbox",
        env_vars_extra={},
        runner_bindings={"OPENAI_API_KEY": "binding-key"},
    )
    assert env["GITHUB_TOKEN"] == "host-pat"
    assert "APP_MODULO_OPENCODE_API_KEY" in env
    assert env["OPENAI_API_KEY"] == "binding-key"


class TestRunnerBindingEnvProfileId:
    def teardown_method(self):
        _conformance_ctx_cv.set(None)

    def test_returns_profile_id_from_conformance_ctx(self):
        profile_id = uuid.uuid4()
        set_conformance_ctx(session_factory=None, org_id=None, environment_profile_id=profile_id, pipeline_id=None)
        assert _runner_binding_env_profile_id() == profile_id

    def test_returns_none_when_ctx_absent(self):
        _conformance_ctx_cv.set(None)
        assert _runner_binding_env_profile_id() is None

    def test_returns_none_when_ctx_too_short(self):
        _conformance_ctx_cv.set((None,))
        assert _runner_binding_env_profile_id() is None

    def test_returns_none_for_unparseable_profile_id(self):
        set_conformance_ctx(session_factory=None, org_id=None, environment_profile_id="not-a-uuid", pipeline_id=None)
        assert _runner_binding_env_profile_id() is None
