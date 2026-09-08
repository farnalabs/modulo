"""``modulo apply`` orchestration + CLI registration (FAR-681, slice 1).

Declarative configuration: ``modulo apply -f config.yaml`` plans name-based
upserts against the live org and executes them idempotently. This slice
covers schemas (+versions) and model_backends; pipelines and triggers land
in slices 2/3.

Exit-code semantics:
- dry-run (--dry-run/--plan): always exit 0
- real apply: exit 1 if any entity was blocked or failed, else 0
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import click
import httpx

from modulo.cli.apply.executor import ApplyHttpError
from modulo.cli.apply.loader import ApplyLoadError, load_apply_file
from modulo.cli.apply.models import ApplyConfig
from modulo.cli.apply.plan import has_blockers

_log = logging.getLogger(__name__)

_ENV_URL = "MODULO_URL"
_ENV_API_KEY = "MODULO_API_KEY"


def run_apply(
    config: ApplyConfig,
    *,
    base_url: str,
    api_key: str,
    dry_run: bool,
    client: Any = None,
) -> dict[str, Any]:
    """Plan (and unless dry-run, execute) an ApplyConfig; returns the report."""
    from modulo.cli.apply.executor import ApplyExecutor

    executor = ApplyExecutor(base_url, api_key, client=client)
    try:
        return executor.run(config, dry_run=dry_run)
    finally:
        executor.close()


def _http_error_message(exc: ApplyHttpError) -> str:
    """Actionable message for executor-phase HTTP failures."""
    if exc.status_code == 401:
        return "API key rejected: check MODULO_API_KEY (an org API key, mk_..., with operator role is required)"
    if exc.status_code == 404:
        target = exc.path or str(exc)
        return f"server does not expose endpoint {target} - is it running an older version?"
    return str(exc)


def render_table(report: dict[str, Any]) -> str:
    """Human-friendly plan/apply report rendering."""
    verbs = {
        "created": "create",
        "updated": "update",
        "blocked": "block",
    }
    lines: list[str] = []
    for status, verb in verbs.items():
        for entry in report.get(status, []):
            line = f"{verb} {entry['kind']} {entry['name']!r}"
            if status == "blocked":
                line += f" ({entry['reason']})"
            lines.append(line)
    lines.extend(f"fail {entry['kind']} {entry['name']!r}: {entry['error']}" for entry in report.get("failed", []))
    lines.extend(f"unchanged {entry['kind']} {entry['name']!r}" for entry in report.get("unchanged", []))
    counts = {s: len(report.get(s, [])) for s in ("created", "updated", "unchanged", "blocked", "failed")}
    lines.append(
        f"summary: {counts['created']} created, {counts['updated']} updated, "
        f"{counts['unchanged']} unchanged, {counts['blocked']} blocked, {counts['failed']} failed"
    )
    return "\n".join(lines)


def register_apply(group: click.Group) -> None:
    """Attach the ``apply`` subcommand to a click group."""

    @group.command("apply")
    @click.option(
        "--file",
        "-f",
        "config_path",
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
        required=True,
        help="YAML apply config (multi-document accepted)",
    )
    @click.option(
        "--dry-run",
        "--plan",
        "dry_run",
        is_flag=True,
        default=False,
        help="Compute and report the plan without applying changes",
    )
    @click.option(
        "--output",
        "output_format",
        type=click.Choice(["json", "table"]),
        default="table",
        help="Report output format",
    )
    @click.option(
        "--json",
        "json_flag",
        is_flag=True,
        default=False,
        help="Alias for --output json",
    )
    def apply_cmd(
        config_path: Path,
        dry_run: bool,
        output_format: str,
        json_flag: bool,
    ) -> None:
        """Apply a declarative config file (schemas, model backends)."""
        try:
            config = load_apply_file(config_path)
        except ApplyLoadError as exc:
            raise click.ClickException(str(exc)) from None
        base_url = os.environ.get(_ENV_URL)
        api_key = os.environ.get(_ENV_API_KEY)
        if not base_url or not api_key:
            msg = f"{_ENV_URL} and {_ENV_API_KEY} environment variables are required"
            raise click.ClickException(msg)
        try:
            report = run_apply(config, base_url=base_url, api_key=api_key, dry_run=dry_run)
        except ApplyHttpError as exc:
            raise click.ClickException(_http_error_message(exc)) from None
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise click.ClickException(f"apply failed: {exc}") from None
        if json_flag:
            output_format = "json"
        if output_format == "json":
            click.echo(json.dumps(report, indent=2, sort_keys=True))
        else:
            click.echo(render_table(report))
        if not dry_run and has_blockers(report):
            ctx = click.get_current_context()
            ctx.exit(1)
