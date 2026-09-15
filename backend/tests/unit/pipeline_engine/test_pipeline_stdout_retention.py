"""Tests for FAR-811: pipeline-level sandbox stdout retention config.

Resolution semantics: node > pipeline > org(ceiling).

- Node-explicit stdout_retention_mode always wins.
- When node didn't set mode, pipeline default's mode/max_bytes are inherited.
- Org ceiling clamps the resolved cap regardless of source.
- validate_stdout_reention_config rejects malformed shapes.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from modulo.core.pipeline_engine.node_runner import (
    _FULL_MODE_DEFAULT_MAX_BYTES,
    _MAX_ARTIFACT_LOG,
    _build_sandbox_node_config,
    _coerce_stdout_max_bytes,
    _coerce_stdout_retention_mode,
    _resolve_stdout_cap,
)
from modulo.core.stdout_retention import validate_stdout_retention_config

# ---------------------------------------------------------------------------
# validate_stdout_retention_config
# ---------------------------------------------------------------------------


class TestValidateStdoutRetentionConfig:
    def test_none_passes_through(self) -> None:
        assert validate_stdout_retention_config(None) is None

    def test_empty_dict_normalizes_to_none(self) -> None:
        # "Set to {} to clear (no pipeline override)" — documented clear op.
        assert validate_stdout_retention_config({}) is None

    def test_valid_full_with_max_bytes(self) -> None:
        result = validate_stdout_retention_config({"mode": "full", "max_bytes": 2048})
        assert result == {"mode": "full", "max_bytes": 2048}

    def test_valid_full_without_max_bytes(self) -> None:
        result = validate_stdout_retention_config({"mode": "full"})
        assert result == {"mode": "full"}

    def test_valid_tail(self) -> None:
        result = validate_stdout_retention_config({"mode": "tail"})
        assert result == {"mode": "tail"}

    def test_rejects_invalid_mode(self) -> None:
        with pytest.raises(ValueError, match="stdout_retention_config"):
            validate_stdout_retention_config({"mode": "invalid"})

    def test_rejects_dict_without_mode(self) -> None:
        with pytest.raises(ValueError, match="stdout_retention_config"):
            validate_stdout_retention_config({"max_bytes": 1024})

    def test_rejects_non_dict(self) -> None:
        with pytest.raises(ValueError, match="stdout_retention_config"):
            validate_stdout_retention_config("tail")

    def test_rejects_negative_max_bytes(self) -> None:
        with pytest.raises(ValueError, match="stdout_max_bytes must be a positive integer"):
            validate_stdout_retention_config({"mode": "full", "max_bytes": -1})

    def test_rejects_zero_max_bytes(self) -> None:
        with pytest.raises(ValueError, match="stdout_max_bytes must be a positive integer"):
            validate_stdout_retention_config({"mode": "full", "max_bytes": 0})

    def test_rejects_bool_max_bytes(self) -> None:
        with pytest.raises(ValueError, match="stdout_max_bytes must be a positive integer"):
            validate_stdout_retention_config({"mode": "full", "max_bytes": True})


# ---------------------------------------------------------------------------
# _resolve_stdout_cap pipeline-default integration
# ---------------------------------------------------------------------------


class TestResolveStdoutCapPipelineDefault:
    """FAR-811: node > pipeline > org resolution in _resolve_stdout_cap."""

    def test_node_explicit_wins_over_pipeline(self) -> None:
        """Node's own mode/max_bytes override pipeline defaults."""
        assert _resolve_stdout_cap("full", 2048) == 2048
        # Even with a pipeline default, the resolved mode/max_bytes already
        # reflect the node's choice — the caller applies pipeline default
        # BEFORE calling _resolve_stdout_cap.

    def test_pipeline_default_full_mode(self) -> None:
        """Pipeline default mode=full with max_bytes overrides tail default."""
        # Caller resolves: node didn't set mode -> pipeline default
        mode = _coerce_stdout_retention_mode({"mode": "full"}.get("mode"))
        max_bytes = _coerce_stdout_max_bytes({"mode": "full", "max_bytes": 4096}.get("max_bytes"))
        assert _resolve_stdout_cap(mode, max_bytes) == 4096

    def test_pipeline_default_tail_mode(self) -> None:
        """Pipeline default mode=tail keeps legacy cap."""
        mode = _coerce_stdout_retention_mode({"mode": "tail"}.get("mode"))
        max_bytes = _coerce_stdout_max_bytes({"mode": "tail"}.get("max_bytes"))
        assert _resolve_stdout_cap(mode, max_bytes) == _MAX_ARTIFACT_LOG

    def test_pipeline_default_clamped_by_org_ceiling(self) -> None:
        """Pipeline default that exceeds org ceiling is clamped."""
        mode = _coerce_stdout_retention_mode({"mode": "full", "max_bytes": 20_000_000}.get("mode"))
        max_bytes = _coerce_stdout_max_bytes({"mode": "full", "max_bytes": 20_000_000}.get("max_bytes"))
        assert _resolve_stdout_cap(mode, max_bytes, org_ceiling=5_000_000) == 5_000_000

    def test_pipeline_default_full_no_max_bytes_uses_5mb(self) -> None:
        """Pipeline default mode=full without max_bytes uses the 5MB default."""
        mode = _coerce_stdout_retention_mode({"mode": "full"}.get("mode"))
        max_bytes = _coerce_stdout_max_bytes({"mode": "full"}.get("max_bytes"))
        assert _resolve_stdout_cap(mode, max_bytes) == _FULL_MODE_DEFAULT_MAX_BYTES


# ---------------------------------------------------------------------------
# _build_sandbox_node_config pipeline-default integration
# ---------------------------------------------------------------------------


class TestBuildSandboxNodeConfigPipelineDefault:
    """FAR-811: pipeline default flows through _build_sandbox_node_config."""

    def _base_node_def(self, **overrides: Any) -> dict[str, Any]:
        base = {
            "id": str(uuid.uuid4()),
            "node_type": "sandbox_agent",
            "mode": "llm",
            "agent_prompt": "Hello {{ input }}",
            "agent_commands": ["echo hello"],
        }
        base.update(overrides)
        return base

    def _build(self, node_def: dict[str, Any], pipeline_cfg: dict[str, Any] | None = None) -> Any:
        return _build_sandbox_node_config(
            node_def,
            session_factory=None,
            single_sandbox_node=False,
            pipeline_stdout_retention_config=pipeline_cfg,
        )

    def test_node_explicit_mode_wins(self) -> None:
        """Node's own stdout_retention_mode overrides pipeline default."""
        node_def = self._base_node_def(stdout_retention_mode="full", stdout_max_bytes=2048)
        config = self._build(node_def, pipeline_cfg={"mode": "tail"})
        assert config.stdout_retention_mode == "full"
        assert config.stdout_max_bytes == 2048

    def test_node_not_set_inherits_pipeline_full(self) -> None:
        """Node without stdout_retention_mode inherits pipeline default."""
        node_def = self._base_node_def()  # no stdout_retention_mode
        config = self._build(node_def, pipeline_cfg={"mode": "full", "max_bytes": 4096})
        assert config.stdout_retention_mode == "full"
        assert config.stdout_max_bytes == 4096

    def test_node_not_set_no_pipeline_defaults_to_tail(self) -> None:
        """Node without stdout_retention_mode and no pipeline default -> tail."""
        node_def = self._base_node_def()
        config = self._build(node_def, pipeline_cfg=None)
        assert config.stdout_retention_mode == "tail"
        assert config.stdout_max_bytes is None

    def test_node_not_set_inherits_pipeline_tail(self) -> None:
        """Node without stdout_retention_mode inherits pipeline tail mode."""
        node_def = self._base_node_def()
        config = self._build(node_def, pipeline_cfg={"mode": "tail"})
        assert config.stdout_retention_mode == "tail"
        assert config.stdout_max_bytes is None

    def test_node_explicit_tail_stays_tail(self) -> None:
        """Node explicitly set to tail stays tail even with pipeline full."""
        node_def = self._base_node_def(stdout_retention_mode="tail")
        config = self._build(node_def, pipeline_cfg={"mode": "full", "max_bytes": 8192})
        assert config.stdout_retention_mode == "tail"
        assert config.stdout_max_bytes is None

    def test_pipeline_config_frozen_on_config_object(self) -> None:
        """The pipeline config is stored on the config for downstream use."""
        pipeline_cfg = {"mode": "full", "max_bytes": 1024}
        node_def = self._base_node_def()
        config = self._build(node_def, pipeline_cfg=pipeline_cfg)
        assert config.pipeline_stdout_retention_config == pipeline_cfg

    def test_node_max_bytes_only_wins_over_pipeline(self) -> None:
        """Node that sets only stdout_max_bytes (no mode) keeps its explicit max_bytes.

        FAR-811: node-explicit settings always win, so a node that sets
        stdout_max_bytes without stdout_retention_mode must not have that value
        discarded in favour of the pipeline default's max_bytes. The mode is
        inherited from the pipeline default.
        """
        node_def = self._base_node_def(stdout_max_bytes=2048)
        config = self._build(node_def, pipeline_cfg={"mode": "full", "max_bytes": 4096})
        assert config.stdout_retention_mode == "full"
        assert config.stdout_max_bytes == 2048

    def test_node_max_bytes_only_no_pipeline_defaults_to_tail(self) -> None:
        """Node that sets only stdout_max_bytes (no mode, no pipeline) is tail."""
        node_def = self._base_node_def(stdout_max_bytes=2048)
        config = self._build(node_def, pipeline_cfg=None)
        assert config.stdout_retention_mode == "tail"
        assert config.stdout_max_bytes == 2048
