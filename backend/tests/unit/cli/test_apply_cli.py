"""Unit tests for the ``modulo apply`` CLI registration (FAR-681 slice 1)."""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
import respx
from click.testing import CliRunner

from modulo.cli.apply import register_apply

CONFIG_TEXT = """
api_version: modulo.dev/v1
entities:
  schemas:
    - name: alpha
      description: Alpha schema
"""

BACKEND_CONFIG_TEXT = """
api_version: modulo.dev/v1
entities:
  model_backends:
    - name: openai
      display_name: OpenAI
      provider: openai
      model_id: gpt-x
      api_key: ${env:SK}
"""


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_TEXT, encoding="utf-8")
    return path


class TestApplyCommandRegistration:
    def test_registered_on_group(self) -> None:
        group = click.Group("test")
        register_apply(group)
        assert "apply" in group.commands

    def test_backup_cli_exports_apply(self) -> None:
        from modulo.cli.backup import cli

        assert "apply" in cli.commands


class TestEnvRequired:
    def test_missing_env_errors(self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODULO_URL", raising=False)
        monkeypatch.delenv("MODULO_API_KEY", raising=False)
        group = click.Group("test")
        register_apply(group)
        result = runner.invoke(group, ["apply", "-f", str(_config_file(tmp_path))])
        assert result.exception is not None or result.exit_code != 0
        assert "MODULO_URL" in result.output or "MODULO_API_KEY" in result.output


class TestDryRun:
    @respx.mock
    def test_dry_run_exits_zero_and_outputs_json(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MODULO_URL", "https://api.test")
        monkeypatch.setenv("MODULO_API_KEY", "key")
        monkeypatch.setenv("SK", "v")
        respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}).respond(
            json={"items": [], "total": 0, "page": 1, "page_size": 100}
        )
        respx.get("https://api.test/api/v1/model-backends", params={"page": "1", "page_size": "100"}).respond(
            json={"items": [], "total": 0, "page": 1, "page_size": 100}
        )
        group = click.Group("test")
        register_apply(group)
        path = _config_file(tmp_path)
        result = runner.invoke(group, ["apply", "-f", str(path), "--dry-run", "--output", "json"])
        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        created_names = [e["name"] for e in parsed["created"]]
        assert created_names == ["alpha"]
        assert not parsed["failed"]

    @respx.mock
    def test_plan_alias_flag(self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_URL", "https://api.test")
        monkeypatch.setenv("MODULO_API_KEY", "key")
        monkeypatch.setenv("SK", "v")
        respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}).respond(
            json={"items": [], "total": 0, "page": 1, "page_size": 100}
        )
        respx.get("https://api.test/api/v1/model-backends", params={"page": "1", "page_size": "100"}).respond(
            json={"items": [], "total": 0, "page": 1, "page_size": 100}
        )
        group = click.Group("test")
        register_apply(group)
        path = _config_file(tmp_path)
        result = runner.invoke(group, ["apply", "-f", str(path), "--plan"])
        assert result.exit_code == 0, result.output
        assert "create schema 'alpha'" in result.output


class TestBlockedExitCode:
    @respx.mock
    def test_real_apply_blocked_exits_1(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SK", raising=False)
        monkeypatch.setenv("MODULO_URL", "https://api.test")
        monkeypatch.setenv("MODULO_API_KEY", "key")
        respx.get("https://api.test/api/v1/schemas", params={"page": "1", "page_size": "100"}).respond(
            json={"items": [], "total": 0, "page": 1, "page_size": 100}
        )
        respx.get("https://api.test/api/v1/model-backends", params={"page": "1", "page_size": "100"}).respond(
            json={"items": [], "total": 0, "page": 1, "page_size": 100}
        )
        backend_post = respx.post("https://api.test/api/v1/model-backends")
        schema_post = respx.post("https://api.test/api/v1/schemas")
        group = click.Group("test")
        register_apply(group)
        path = tmp_path / "config.yaml"
        path.write_text(BACKEND_CONFIG_TEXT, encoding="utf-8")
        result = runner.invoke(group, ["apply", "-f", str(path)])
        assert result.exit_code == 1, result.output
        assert backend_post.call_count == 0
        assert schema_post.call_count == 0
        assert "unresolved" in result.output
