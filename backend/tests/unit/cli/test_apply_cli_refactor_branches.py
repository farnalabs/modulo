"""Focused coverage for the S3776 decomposition of ``modulo.cli.apply``."""

from pathlib import Path

import click
from click.testing import CliRunner

from modulo.cli.apply import register_apply


def _group() -> click.Group:
    group = click.Group("test")
    register_apply(group)
    return group


def test_load_config_failure_surfaces_as_click_exception(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("api_version: [unterminated", encoding="utf-8")

    result = CliRunner().invoke(_group(), ["apply", "--file", str(config_path)])

    assert result.exit_code != 0
    assert "yaml parse error" in result.output.lower()
