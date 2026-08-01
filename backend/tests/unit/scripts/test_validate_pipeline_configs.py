"""Unit tests for validate_pipeline_configs.py — sandbox_agent config linting."""

from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest.mock import patch

import pytest

for parent in Path(__file__).resolve().parents:
    script_path = parent / "scripts" / "validate_pipeline_configs.py"
    if script_path.exists():
        break
else:
    raise RuntimeError("Could not find repo root (scripts/validate_pipeline_configs.py)")

_vpc_loader = SourceFileLoader("validate_pipeline_configs", str(script_path))
vpc = module_from_spec(spec_from_loader("validate_pipeline_configs", _vpc_loader))
_vpc_loader.exec_module(vpc)


@pytest.fixture(autouse=True)
def reset_exit_code():
    vpc._EXIT_CODE = 0
    yield
    vpc._EXIT_CODE = 0


def _good_node() -> dict:
    return {
        "node_type": "sandbox_agent",
        "agent_prompt": "review the code",
        "template_id": "opencode",
        "timeout_seconds": 1200,
        "envs": {"env_vars_extra": {"EXTRA": "1"}, "GITHUB_TOKEN": "t"},
    }


# ---------------------------------------------------------------------------
# _fail
# ---------------------------------------------------------------------------


def test_fail_sets_exit_code_and_prints_to_stderr(capsys):
    vpc._fail("something went wrong")
    assert vpc._EXIT_CODE == 1
    assert "FAIL: something went wrong" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# _check_node_config
# ---------------------------------------------------------------------------


def test_check_node_config_ok_no_failures(tmp_path):
    with patch.object(vpc, "_fail") as fail:
        vpc._check_node_config(Path(tmp_path), _good_node())
    fail.assert_not_called()


def test_check_node_config_empty_agent_prompt_fails(tmp_path):
    node = _good_node()
    node["agent_prompt"] = ""
    with patch.object(vpc, "_fail") as fail:
        vpc._check_node_config(Path(tmp_path), node)
    fail.assert_called_once()
    assert "agent_prompt is empty" in fail.call_args.args[0]


def test_check_node_config_non_opencode_template_fails(tmp_path):
    node = _good_node()
    node["template_id"] = "base"
    with patch.object(vpc, "_fail") as fail:
        vpc._check_node_config(Path(tmp_path), node)
    assert "template_id is 'base'" in fail.call_args.args[0]


def test_check_node_config_default_timeout_fails(tmp_path):
    node = _good_node()
    node["timeout_seconds"] = 600
    with patch.object(vpc, "_fail") as fail:
        vpc._check_node_config(Path(tmp_path), node)
    assert "timeout_seconds is 600" in fail.call_args.args[0]


def test_check_node_config_env_vars_extra_after_token_fails(tmp_path):
    node = _good_node()
    node["envs"] = {"GITHUB_TOKEN": "t", "env_vars_extra": {"EXTRA": "1"}}
    with patch.object(vpc, "_fail") as fail:
        vpc._check_node_config(Path(tmp_path), node)
    assert "env_vars_extra" in fail.call_args.args[0]


def test_check_node_config_env_vars_extra_first_ok(tmp_path):
    node = _good_node()
    node["envs"] = {"env_vars_extra": {"EXTRA": "1"}, "APP_MODULO_OPENCODE_API_KEY": "k"}
    with patch.object(vpc, "_fail") as fail:
        vpc._check_node_config(Path(tmp_path), node)
    fail.assert_not_called()


def test_check_node_config_non_sandbox_node_ignored(tmp_path):
    node = {"node_type": "agent", "agent_prompt": "", "template_id": "base", "timeout_seconds": 600}
    with patch.object(vpc, "_fail") as fail:
        vpc._check_node_config(Path(tmp_path), node)
    fail.assert_not_called()


def test_check_node_config_recurses_into_children(tmp_path):
    tree = {
        "node_type": "pipeline",
        "nodes": [
            {
                "node_type": "sandbox_agent",
                "agent_prompt": "",
                "template_id": "opencode",
                "timeout_seconds": 1200,
            }
        ],
    }
    with patch.object(vpc, "_fail") as fail:
        vpc._check_node_config(Path(tmp_path), tree)
    fail.assert_called_once()
    assert "[0]" in fail.call_args.args[0]
    assert "agent_prompt is empty" in fail.call_args.args[0]


def test_check_node_config_uses_env_vars_alias(tmp_path):
    node = _good_node()
    node.pop("envs")
    node["env_vars"] = {"GITHUB_TOKEN": "t", "env_vars_extra": {"EXTRA": "1"}}
    with patch.object(vpc, "_fail") as fail:
        vpc._check_node_config(Path(tmp_path), node)
    assert "env_vars_extra" in fail.call_args.args[0]


# ---------------------------------------------------------------------------
# _scan_node_runner
# ---------------------------------------------------------------------------


def test_scan_node_runner_flags_timeout_600_default(tmp_path):
    src = tmp_path / "node_runner.py"
    src.write_text('node_def.get("timeout_seconds", 600)\n')
    with patch.object(vpc, "_fail") as fail:
        vpc._scan_node_runner(src)
    fail.assert_called_once()
    assert "timeout_seconds defaults to 600" in fail.call_args.args[0]


def test_scan_node_runner_flags_template_base_default(tmp_path):
    src = tmp_path / "node_runner.py"
    src.write_text('node_def.get("template_id", "base")\n')
    with patch.object(vpc, "_fail") as fail:
        vpc._scan_node_runner(src)
    fail.assert_called_once()
    assert "template_id defaults to 'base'" in fail.call_args.args[0]


def test_scan_node_runner_ok_on_good_defaults(tmp_path):
    src = tmp_path / "node_runner.py"
    src.write_text('node_def.get("timeout_seconds", 1200)\nnode_def.get("template_id", "opencode")\n')
    with patch.object(vpc, "_fail") as fail:
        vpc._scan_node_runner(src)
    fail.assert_not_called()


def test_scan_node_runner_flags_env_vars_extra_line(tmp_path):
    src = tmp_path / "node_runner.py"
    src.write_text('envs = {\n    "GITHUB_TOKEN": "x",\n    **env_vars_extra\n}\n')
    with patch.object(vpc, "_fail") as fail:
        vpc._scan_node_runner(src)
    fail.assert_called_once()
    assert "**env_vars_extra` must precede system env vars" in fail.call_args.args[0]


# ---------------------------------------------------------------------------
# _scan_mcp_server
# ---------------------------------------------------------------------------


def test_scan_mcp_server_flags_empty_agent_prompt(tmp_path):
    src = tmp_path / "mcp_server.py"
    src.write_text('prompt = n.get("agent_prompt", "")\n')
    with patch.object(vpc, "_fail") as fail:
        vpc._scan_mcp_server(src)
    fail.assert_called_once()
    assert "Empty agent_prompt fallback" in fail.call_args.args[0]


def test_scan_mcp_server_ok_with_non_empty_fallback(tmp_path):
    src = tmp_path / "mcp_server.py"
    src.write_text('prompt = n.get("agent_prompt", default_prompt)\n')
    with patch.object(vpc, "_fail") as fail:
        vpc._scan_mcp_server(src)
    fail.assert_not_called()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_returns_zero_when_all_checks_pass(capsys):
    with (
        patch.object(vpc, "_scan_node_runner") as scan_node,
        patch.object(vpc, "_scan_mcp_server") as scan_mcp,
        patch.object(vpc, "_scan_pipeline_config_files") as scan_files,
    ):
        result = vpc.main()

    assert result == 0
    assert "All pipeline config checks passed." in capsys.readouterr().out
    scan_node.assert_called_once()
    scan_mcp.assert_called_once()
    scan_files.assert_called_once()


def test_main_returns_one_when_validations_fail(capsys):
    def bad_scan(path):
        vpc._fail(f"{path}: test failure")

    with (
        patch.object(vpc, "_scan_node_runner", side_effect=bad_scan),
        patch.object(vpc, "_scan_mcp_server"),
        patch.object(vpc, "_scan_pipeline_config_files"),
    ):
        result = vpc.main()

    assert result == 1
    out = capsys.readouterr().err
    assert "test failure" in out
    assert "Some pipeline config validations failed" in out


def test_scan_pipeline_config_files_tolerates_missing_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with patch.object(vpc, "_fail") as fail:
        vpc._scan_pipeline_config_files()
    fail.assert_not_called()


def test_scan_pipeline_config_files_flags_bad_json(monkeypatch, tmp_path):
    pipeline_dir = tmp_path / "pipelines"
    pipeline_dir.mkdir()
    bad = {
        "name": "bad-pipeline",
        "nodes": [
            {
                "node_type": "sandbox_agent",
                "agent_prompt": "",
                "template_id": "base",
                "timeout_seconds": 600,
            }
        ],
    }
    import json

    (pipeline_dir / "bad.json").write_text(json.dumps(bad))
    monkeypatch.chdir(tmp_path)
    with patch.object(vpc, "_fail") as fail:
        vpc._scan_pipeline_config_files()
    assert fail.call_count == 3
    messages = " ".join(c.args[0] for c in fail.call_args_list)
    assert "agent_prompt is empty" in messages
    assert "template_id is 'base'" in messages
    assert "timeout_seconds is 600" in messages


def test_scan_pipeline_config_files_flags_env_vars_extra_order(monkeypatch, tmp_path):
    pipeline_dir = tmp_path / "pipelines"
    pipeline_dir.mkdir()
    bad = {
        "nodes": [
            {
                "type": "sandbox_agent",
                "agent_prompt": "do it",
                "template_id": "opencode",
                "timeout_seconds": 1200,
                "envs": {"GITHUB_TOKEN": "t", "env_vars_extra": {"EXTRA": "1"}},
            }
        ]
    }
    import json

    (pipeline_dir / "bad.json").write_text(json.dumps(bad))
    monkeypatch.chdir(tmp_path)
    with patch.object(vpc, "_fail") as fail:
        vpc._scan_pipeline_config_files()
    fail.assert_called_once()
    assert "env_vars_extra" in fail.call_args.args[0]


def test_scan_pipeline_config_files_ignores_good_json(monkeypatch, tmp_path):
    pipeline_dir = tmp_path / "pipelines"
    pipeline_dir.mkdir()
    good = {
        "nodes": [
            {
                "type": "sandbox_agent",
                "agent_prompt": "do it",
                "template_id": "opencode",
                "timeout_seconds": 1200,
                "envs": {"env_vars_extra": {"EXTRA": "1"}, "GITHUB_TOKEN": "t"},
            }
        ]
    }
    import json

    (pipeline_dir / "good.json").write_text(json.dumps(good))
    monkeypatch.chdir(tmp_path)
    with patch.object(vpc, "_fail") as fail:
        vpc._scan_pipeline_config_files()
    fail.assert_not_called()
