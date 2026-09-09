"""Migrate existing CompositeTemplate parameter_ports_json to ParameterSchema + ParameterSet.

Reads all composite templates that have parameter_ports_json but no
parameter_schema_id, creates a ParameterSchema and default ParameterSet
for each, and links the composite to the new schema.

Usage:
    uv run python tools/migrate_composite_parameters.py --dry-run
    uv run python tools/migrate_composite_parameters.py --verbose
    uv run python tools/migrate_composite_parameters.py   # actually migrates
"""

import argparse
import asyncio
import logging
import sys
import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_log = logging.getLogger(__name__)


def _print(*args: object, **kwargs: Any) -> None:
    print(*args, **kwargs)  # noqa: T201


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate composite template parameters to ParameterSchema")
    parser.add_argument("--dry-run", action="store_true", help="Only read, don't write")
    parser.add_argument("--verbose", action="store_true", help="Print per-composite details")
    return parser.parse_args()


def _print_template_details(
    template_name: str,
    template_id: uuid.UUID,
    schema_name: str,
    ports_raw: list[dict[str, Any]],
) -> None:
    _print(f"\n  Template: {template_name} ({template_id})")
    _print(f"    Schema name: {schema_name}")
    _print(f"    Ports ({len(ports_raw)}):")
    for p in ports_raw:
        _print(f"      - {p.get('name', '?')}: {p.get('type', '?')}")


def _default_values(ports_raw: list[dict[str, Any]]) -> dict[str, object]:
    default_values: dict[str, object] = {}
    for p in ports_raw:
        if "default_value" in p and p["default_value"] is not None:
            default_values[p["name"]] = p["default_value"]
    return default_values


async def _migrate_one_template(
    session_factory: async_sessionmaker[AsyncSession],
    template_id: uuid.UUID,
    template_name: str,
    org_id: uuid.UUID,
    account_id: uuid.UUID,
    schema_name: str,
    ports_raw: list[dict[str, Any]],
) -> tuple[uuid.UUID, uuid.UUID]:
    from modulo.db.models.parameter_schema import ParameterSchema

    async with session_factory() as session, session.begin():
        schema = ParameterSchema(
            organisation_id=org_id,
            name=schema_name,
            description=f"Migrated from composite template '{template_name}'",
            parameters=ports_raw,
            account_id=account_id,
        )
        session.add(schema)
        await session.flush()

        from modulo.db.models.parameter_set import ParameterSet

        param_set = ParameterSet(
            parameter_schema_id=schema.id,
            organisation_id=org_id,
            account_id=account_id,
            name="Default",
            description=f"Default parameter set for '{template_name}'",
            values=_default_values(ports_raw),
            schema_version=1,
        )
        session.add(param_set)
        await session.flush()

        await session.execute(
            text("UPDATE composite_templates SET parameter_schema_id = :schema_id WHERE id = :template_id"),
            {"schema_id": schema.id, "template_id": template_id},
        )

    return schema.id, param_set.id


async def _migrate_row(
    args: argparse.Namespace,
    session_factory: async_sessionmaker[AsyncSession],
    row: Mapping[Any, Any],
) -> tuple[int, int, int]:
    """Migrate a single composite-template row.

    Returns ``(migrated, would_migrate, errors)`` deltas.
    """
    template_id: uuid.UUID = row["id"]
    template_name: str = row["name"]
    org_id: uuid.UUID = row["organisation_id"]
    account_id: uuid.UUID = row["account_id"]
    ports_raw: list[dict[str, Any]] = row["parameter_ports_json"]

    if not ports_raw:
        return (0, 0, 0)

    schema_name = f"{template_name} Parameters"

    if args.verbose:
        _print_template_details(template_name, template_id, schema_name, ports_raw)

    if args.dry_run:
        return (0, 1, 0)

    try:
        schema_id, param_set_id = await _migrate_one_template(
            session_factory, template_id, template_name, org_id, account_id, schema_name, ports_raw
        )
        if args.verbose:
            _print(f"    -> Created schema {schema_id}, set {param_set_id}")
    except Exception as exc:
        _log.exception("Failed to migrate template %s (%s)", template_id, template_name)
        _print(f"    ERROR: {exc}", file=sys.stderr)
        return (0, 0, 1)

    return (1, 0, 0)


async def _migrate(args: argparse.Namespace) -> int:
    from modulo.settings import get_settings

    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        result = await session.execute(
            text("""
                SELECT id, name, organisation_id, account_id, parameter_ports_json
                FROM composite_templates
                WHERE parameter_ports_json IS NOT NULL
                  AND parameter_ports_json != '[]'::jsonb
                  AND parameter_schema_id IS NULL
            """)
        )
        rows = list(result.mappings().all())

    if not rows:
        _print("No composite templates found to migrate.")
        await engine.dispose()
        return 0

    _print(f"Found {len(rows)} composite template(s) to migrate.")

    migrated = 0
    would_migrate = 0
    errors = 0

    for row in rows:
        migrated_delta, would_delta, error_delta = await _migrate_row(args, session_factory, row)
        migrated += migrated_delta
        would_migrate += would_delta
        errors += error_delta

    await engine.dispose()

    if args.dry_run:
        _print(f"\nDRY RUN complete. {would_migrate} would be migrated, {errors} errors.")
    else:
        _print(f"\nDone. {migrated} migrated, {errors} errors.")
    return 0 if errors == 0 else 1


def main() -> None:
    args = _parse_args()
    if args.dry_run:
        _print("DRY RUN — no changes will be written\n")
    exit_code = asyncio.run(_migrate(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
