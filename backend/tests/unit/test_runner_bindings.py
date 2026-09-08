"""FAR-592 (D6): per-agent Model Backend env-var runner-bindings — unit tests."""

from __future__ import annotations

import pytest

from modulo.core.pipeline_engine.node_runner import _build_sandbox_envs
from modulo.core.runner_bindings import (
    RESERVED_ENV_VAR_PREFIXES,
    RESERVED_ENV_VARS,
    is_reserved_env_var,
)
from modulo.db.runner_binding_constraints import (
    BindingValidationError,
    known_source_fields_for,
    validate_binding_pair,
    validate_target_env_var,
)


class TestReservedEnvVarConstant:
    """The reserved list is a NAMED, TESTED constant (denylist limitation documented in docs)."""

    @pytest.mark.parametrize(
        "name",
        [
            pytest.param("MODULO_API_KEY", id="modulo_minted_runner_key"),
            pytest.param("GITHUB_TOKEN", id="github_pat_node_wins_override"),
            pytest.param("LD_PRELOAD", id="loader_injective"),
            pytest.param("LD_LIBRARY_PATH", id="loader_library_path"),
            pytest.param("PATH", id="path"),
            pytest.param("NODE_OPTIONS", id="node_options"),
            pytest.param("PYTHONPATH", id="pythonpath"),
            pytest.param("PYTHONSTARTUP", id="pythonstartup"),
            pytest.param("BASH_ENV", id="bash_env"),
            pytest.param("ENV", id="env"),
            pytest.param("DOCKER_HOST", id="docker_host_redirection"),
        ],
    )
    def test_named_reserved_var_is_denied(self, name: str) -> None:
        assert name in RESERVED_ENV_VARS
        assert is_reserved_env_var(name)

    def test_git_proxy_config_family_denied_by_prefix(self) -> None:
        assert "GIT_PROXY_COMMAND" not in RESERVED_ENV_VARS  # family matches via prefix
        assert is_reserved_env_var("GIT_PROXY_COMMAND")
        assert is_reserved_env_var("GIT_CONFIG_GLOBAL")
        assert is_reserved_env_var("GIT_SSL_NO_VERIFY")
        assert RESERVED_ENV_VAR_PREFIXES == ("MODULO_", "APP_MODULO_", "GIT_")

    def test_modulo_prefix_family_denied(self) -> None:
        assert is_reserved_env_var("MODULO_RUN_ID")
        assert is_reserved_env_var("APP_MODULO_OPENCODE_API_KEY")

    def test_application_credential_is_allowed(self) -> None:
        assert not is_reserved_env_var("OPENCODE_API_KEY")
        assert not is_reserved_env_var("STRIPE_API_KEY")


class TestTargetEnvVarValidation:
    def test_valid_name_canonical(self) -> None:
        # The canonical form is UPPERCASE (server-side canonicalisation —
        # the UNIQUE constraint must be case-variant-proof).
        assert validate_target_env_var("OPENCODE_API_KEY") == "OPENCODE_API_KEY"
        assert validate_target_env_var(" _var1 ") == "_VAR1"
        assert validate_target_env_var("opencode_api_key") == "OPENCODE_API_KEY"

    def test_empty_rejected(self) -> None:
        with pytest.raises(BindingValidationError):
            validate_target_env_var("   ")

    @pytest.mark.parametrize("bad", ["OPEN-API-KEY", "MY.VAR", "1VAR", "HAVE SPACE"])
    def test_malformed_characters_rejected(self, bad: str) -> None:
        with pytest.raises(BindingValidationError):
            validate_target_env_var(bad)

    def test_overlong_rejected(self) -> None:
        with pytest.raises(BindingValidationError):
            validate_target_env_var("A" * 129)

    def test_reserved_rejected(self) -> None:
        with pytest.raises(BindingValidationError, match="reserved"):
            validate_target_env_var("MODULO_API_KEY")
        with pytest.raises(BindingValidationError, match="reserved"):
            validate_binding_pair(target_env_var="GIT_PROXY_COMMAND", source_field="api_key", provider="openai")

    def test_unknown_source_field_rejected(self) -> None:
        with pytest.raises(BindingValidationError, match="known credential field"):
            validate_binding_pair(target_env_var="AZURE_KEY", source_field="root_password", provider="openai")

    def test_unresolvable_rich_field_rejected(self) -> None:
        """Fields the backend write path never stores must fail at SAVE time.

        The secrets backend stores exactly ``{"api_key": ...}`` per backend; a
        save-time-accepted ``aws_access_key_id`` would be a provision-time
        resolution failure every run (the trap save validation exists to
        prevent).
        """
        with pytest.raises(BindingValidationError, match="known credential field"):
            validate_binding_pair(target_env_var="AWS_KEY", source_field="aws_access_key_id", provider="bedrock")


def test_provider_field_surface() -> None:
    """The resolvable surface is uniformly ``api_key`` (the only stored secret)."""
    for provider in ("openai", "anthropic", "bedrock", "vertexai", "custom"):
        assert set(known_source_fields_for(provider)) == {"api_key"}


class TestErrorCodeMapping:
    """Resolution failures are first-class error codes (the D6 rollback trigger)."""

    def test_registry_entries(self) -> None:
        from modulo.core.pipeline_engine.error_codes import ERROR_CODE_REGISTRY

        binding_spec = ERROR_CODE_REGISTRY["sandbox.binding_resolution"]
        assert binding_spec.error_class == "config"
        assert binding_spec.retryable is True
        tier_spec = ERROR_CODE_REGISTRY["sandbox.tier_refused"]
        assert tier_spec.error_class == "config"
        assert tier_spec.retryable is False

    def test_legacy_aliases_map_class_names(self) -> None:
        """Both the node-runner wrapper and the raw core class name resolve."""
        from modulo.core.pipeline_engine.error_codes import LEGACY_ALIASES, map_legacy_code

        assert LEGACY_ALIASES["SandboxBindingResolutionError"] == "sandbox.binding_resolution"
        assert LEGACY_ALIASES["SandboxTierRefusedError"] == "sandbox.tier_refused"
        assert map_legacy_code("SandboxBindingResolutionError") == "sandbox.binding_resolution"
        assert map_legacy_code("SandboxTierRefusedError") == "sandbox.tier_refused"

    def test_typed_error_classes_are_sandbox_node_failures(self) -> None:
        """The node raises SandboxNodeFailedError subclasses (A6 propagation).

        Pinned against the SPECIFIC parent class (not ``Exception``) — the
        subclass contract is what routes these through the executor's
        retryable-node-failure handling before the error-code table.
        """
        from modulo.core.pipeline_engine.node_runner import (
            SandboxBindingResolutionError,
            SandboxNodeFailedError,
            SandboxTierRefusedError,
        )

        assert issubclass(SandboxBindingResolutionError, SandboxNodeFailedError)
        assert issubclass(SandboxTierRefusedError, SandboxNodeFailedError)
        # The LEGACY_ALIASES keys must be the RAISED class names (the executor
        # maps via ``type(exc).__name__``).
        assert SandboxBindingResolutionError.__name__ == "SandboxBindingResolutionError"
        assert SandboxTierRefusedError.__name__ == "SandboxTierRefusedError"


def test_precedence_runner_bindings_between_profile_and_node() -> None:
    """DELIBERATE precedence: profile secrets < runner bindings < node env_vars_extra."""
    envs = _build_sandbox_envs(
        run_id="r1",
        pipeline_id="p1",
        org_id="o1",
        input_json="{}",
        sandbox_mode="llm",
        env_vars_extra={"CUSTOM": "node"},
        runner_bindings={"CUSTOM": "binding", "OPENCODE_API_KEY": "stanza-key"},
    )
    assert envs["MODULO_RUN_ID"] == "r1"
    assert "APP_MODULO_OPENCODE_API_KEY" in envs  # profile/host creds present
    assert envs["CUSTOM"] == "node"  # THE NODE WINS
    assert envs["OPENCODE_API_KEY"] == "stanza-key"
    assert envs["MODULO_ORG_ID"] == "o1"  # far-296 reserved path untouched

    # Script mode gets NO host creds but still merges bindings + extras.
    script_envs = _build_sandbox_envs(
        run_id="r2",
        pipeline_id="p2",
        org_id="o2",
        input_json="{}",
        sandbox_mode="script",
        env_vars_extra={},
        runner_bindings={"K": "v"},
    )
    assert script_envs["K"] == "v"
    assert "APP_MODULO_OPENCODE_API_KEY" not in script_envs
