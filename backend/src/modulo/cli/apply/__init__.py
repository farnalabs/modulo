"""``modulo apply`` orchestration + CLI registration (FAR-681, slices 1-3).

Declarative configuration: ``modulo apply -f config.yaml`` plans name-based
upserts against the live org and executes them idempotently. Slices 1+2
cover schemas (+versions), model_backends, pipelines (agent name-refs in
graphs) and triggers ((pipeline, name) identity); slice 3 adds --diff.
Exit-code semantics:
- dry-run (--dry-run/--plan): always exit 0
- drift mode (--diff): exit 0 when the org matches the config (all
  unchanged), exit 1 when drift is detected (created/updated/blocked) so
  CI can gate on config drift
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
    refresh_secrets: bool = False,
    drift: bool = False,
) -> dict[str, Any]:
    """Plan (and unless dry-run, execute) an ApplyConfig; returns the report."""
    from modulo.cli.apply.executor import ApplyExecutor

    executor = ApplyExecutor(base_url, api_key, client=client)
    try:
        return executor.run(config, dry_run=dry_run, refresh_secrets=refresh_secrets, drift=drift)
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
    """Human-friendly plan/apply/drift report rendering.

    Drift reports (``mode == "drift"``) are explicitly labelled: each verb
    is prefixed (``drift create`` / ``drift update``), the summary line reads
    ``drift summary``, and pipelines in ``drift_detail`` get a graph
    breakdown line.
    """
    drift_mode = report.get("mode") == "drift"
    verbs = {
        "created": "drift create" if drift_mode else "create",
        "updated": "drift update" if drift_mode else "update",
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
    for name, breakdown in sorted((report.get("drift_detail") or {}).items()):
        nodes, edges = breakdown["nodes"], breakdown["edges"]

        def _counts(section: dict[str, list[str]]) -> str:
            return f"+{len(section['added'])}/-{len(section['removed'])}/~{len(section['modified'])}"

        lines.append(f"drift detail pipeline {name!r}: graph {_counts(nodes)} nodes, {_counts(edges)} edges")
    counts = {s: len(report.get(s, [])) for s in ("created", "updated", "unchanged", "blocked", "failed")}
    label = "drift summary" if drift_mode else "summary"
    lines.append(
        f"{label}: {counts['created']} created, {counts['updated']} updated, "
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
        "--refresh-secrets",
        "refresh_secrets",
        is_flag=True,
        default=False,
        help=(
            "Always re-send config_json for triggers declaring secret-shaped entries. "
            "The server masks stored secrets on read, so a rotated ${env:SECRET} value "
            "is invisible to the drift hash (the plan reports 'unchanged' and no PUT "
            "is sent) — this flag re-sends those configs every run."
        ),
    )
    @click.option(
        "--diff",
        "diff_mode",
        is_flag=True,
        default=False,
        help=(
            "Drift mode: read-only comparison of the live org against the config "
            "(same managed-field hashes as the plan). Writes NOTHING. Exit code 0 "
            "when the org matches the config, 1 when drift is detected "
            "(created/updated/blocked) so CI can gate on config drift."
        ),
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
        refresh_secrets: bool,
        diff_mode: bool,
        output_format: str,
        json_flag: bool,
    ) -> None:
        """Apply a declarative config file (schemas, model backends, pipelines, triggers)."""
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
            report = run_apply(
                config,
                base_url=base_url,
                api_key=api_key,
                dry_run=dry_run,
                refresh_secrets=refresh_secrets,
                drift=diff_mode,
            )
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
        if diff_mode:
            from modulo.cli.apply.drift import has_drift

            if has_drift(report):
                ctx = click.get_current_context()
                ctx.exit(1)
        elif not dry_run and has_blockers(report):
            ctx = click.get_current_context()
            ctx.exit(1)
