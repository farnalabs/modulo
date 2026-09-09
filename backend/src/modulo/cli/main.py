"""Top-level ``modulo`` CLI group (FAR-671 slice 2).

Publishes the native-launcher surface (``start``/``stop``/``status``/
``version``/``env``) while keeping the published backup/restore/apply
surface working VERBATIM: the commands from ``modulo.cli.backup`` are
lifted onto this group and an unrecognised first token falls back to
``backup``, so bare ``modulo backup ...`` / ``modulo restore ...`` (and
even ``modulo --db-url ... --output-dir ...``) behave exactly as they did
when ``backup:cli`` WAS the entry point (locked by ``tests/unit/cli/
test_main_group.py``).

The group callback scrubs the OS environment as defence-in-depth after the
import-time graph has loaded (the real first-boot scrub runs inside
``modulo.launcher.entry.run_start`` BEFORE any project import).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import click

from modulo.cli.backup import cli as _legacy_backup_cli

_DEFAULT_COMMAND = "backup"
_PACKAGE_NAME = "farnalabs-modulo"

_SENSITIVE_FIELD_TOKENS = ("password", "secret", "token", "api_key", "private_key")
_SENSITIVE_FIELD_NAMES = frozenset({"secret_key", "fernet_key", "fernet_key_old"})
_URL_FIELD_SUFFIX = "_url"
_REDACTED = "<redacted>"
_CREDENTIAL_IN_URL_RE = re.compile(r"(//)([^@/\s]+)@")


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version(_PACKAGE_NAME)
    except Exception:
        return "unknown"


def _print_version() -> None:
    click.echo(f"modulo {_package_version()}")


class ModuloGroup(click.Group):
    """Top-level group where an unrecognised first token defaults to backup."""

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args and args[0] not in self.commands:
            args = [_DEFAULT_COMMAND, *args]
        return super().resolve_command(ctx, args)


@click.group(cls=ModuloGroup, context_settings={"ignore_unknown_options": True})
@click.option(
    "--version",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=lambda ctx, _param, value: _eager_version(ctx, value),
    help="Print the modulo version and exit.",
)
def cli() -> None:
    """Modulo command line: start/stop/status plus backup and restore."""
    from modulo.launcher.env_safety import scrub_os_environment

    scrub_os_environment()


def _eager_version(ctx: click.Context, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    _print_version()
    ctx.exit(0)


def _register_legacy_commands() -> None:
    for name, command in _legacy_backup_cli.commands.items():
        cli.add_command(command, name=name)


_register_legacy_commands()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _resolve_data_dir(data_dir: Path | None) -> Path:
    from modulo.launcher.entry import default_data_dir

    if data_dir is not None:
        return data_dir
    try:
        return default_data_dir()
    except RuntimeError as exc:
        raise click.ClickException(f"{exc} (pass --data-dir explicitly)") from exc


def _redact_url_credentials(url: str) -> str:
    return _CREDENTIAL_IN_URL_RE.sub(r"\1" + _REDACTED + "@", url)


def _is_sensitive_field(name: str) -> bool:
    lowered = name.lower()
    if name in _SENSITIVE_FIELD_NAMES:
        return True
    return any(token in lowered for token in _SENSITIVE_FIELD_TOKENS)


def _redacted_settings_dump(settings: Any) -> dict[str, str]:
    dump: dict[str, str] = {}
    for name in type(settings).model_fields:
        if _is_sensitive_field(name):
            dump[name] = _REDACTED
            continue
        value = getattr(settings, name)
        if name.endswith(_URL_FIELD_SUFFIX) and isinstance(value, str):
            dump[name] = _redact_url_credentials(value)
            continue
        dump[name] = str(value)
    return dump


# ---------------------------------------------------------------------------
# Native launcher commands
# ---------------------------------------------------------------------------


@cli.command("start")
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Data dir override (default: the per-OS launcher root).",
)
@click.option(
    "--detach",
    is_flag=True,
    default=False,
    help="Fork to the background (POSIX only); logs land in the data dir.",
)
@click.option(
    "--bin-dir",
    type=click.Path(path_type=Path),
    default=None,
    hidden=True,
    help="Bundled-binaries dir override (packaging seam).",
)
@click.pass_context
def start(ctx: click.Context, data_dir: Path | None, detach: bool, bin_dir: Path | None) -> None:
    """Boot the single-install stack: bundled Postgres/Redis, SAQ, and the API."""
    from modulo.launcher.entry import run_start

    try:
        code = run_start(data_dir, detach=detach, bin_dir=bin_dir)
    except KeyboardInterrupt:
        ctx.exit(0)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    ctx.exit(code)


@cli.command("stop")
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Data dir override (default: the per-OS launcher root).",
)
@click.pass_context
def stop(ctx: click.Context, data_dir: Path | None) -> None:
    """Stop the launcher holding the data dir (SIGTERM, ordered teardown)."""
    from modulo.launcher.supervisor import request_stop

    resolved = _resolve_data_dir(data_dir)
    try:
        code = request_stop(resolved)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    ctx.exit(code)


@cli.command("status")
@click.option(
    "--data-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Data dir override (default: the per-OS launcher root).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def status(data_dir: Path | None, as_json: bool) -> None:
    """Show per-component state (postgres/redis/api/workers) for the data dir."""
    from modulo.launcher.supervisor import collect_status

    payload = collect_status(_resolve_data_dir(data_dir))
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    click.echo(f"data dir: {payload.get('data_dir', '?')}")
    if payload.get("error"):
        click.echo(f"error: {payload['error']}")
    click.echo(f"initialized: {payload.get('initialized', False)}")
    launcher = payload.get("launcher")
    if isinstance(launcher, dict):
        click.echo(
            f"launcher: pid={launcher.get('pid')} mode={launcher.get('mode')} "
            f"alive={str(launcher.get('alive')).lower()}"
        )
    components = payload.get("components", {})
    if not isinstance(components, dict):
        components = {}
    click.echo(f"{'component':<12} {'pid':>8} {'port':>8} alive")
    for name in sorted(components):
        component = components[name]
        if not isinstance(component, dict):
            continue
        pid = component.get("pid")
        port = component.get("port")
        click.echo(
            f"{name:<12} {pid if pid is not None else '-'!s:>8} "
            f"{port if port is not None else '-'!s:>8} {str(bool(component.get('alive'))).lower()}"
        )


@cli.command("version")
def version_cmd() -> None:
    """Print the modulo version."""
    _print_version()


@cli.command("env")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def env_cmd(as_json: bool) -> None:
    """Print the effective Settings with every credential redacted."""
    from modulo.settings import get_settings

    try:
        settings = get_settings()
    except Exception as exc:
        raise click.ClickException(f"settings unavailable: {exc}") from exc
    dump = _redacted_settings_dump(settings)
    if as_json:
        click.echo(json.dumps(dump, indent=2, sort_keys=True))
        return
    for key in sorted(dump):
        click.echo(f"{key}={dump[key]}")
