"""modulo-migrate: CLI tool for org data migration (export/import/verify).

Usage:
  modulo-migrate export-org <org-id> --output ./export.jsonl [--pipelines-only] [--users-only]
  modulo-migrate import-org <org-id> --input ./export.jsonl [--on-conflict skip|overwrite|merge]
  modulo-migrate verify-export <org-id> --input ./export.jsonl
"""

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import click
from sqlalchemy import select
from tqdm import tqdm  # type: ignore[import-untyped]

from modulo.auth.jwt import decode_principal
from modulo.db.crud.account import get_account_by_id
from modulo.db.crud.org_membership import get_membership_by_account_and_org
from modulo.db.crud.organisation import get_organisation
from modulo.db.models.account import Account
from modulo.db.models.audit_event import AuditEvent
from modulo.db.models.connector_instance import ConnectorInstance
from modulo.db.models.library_primitive import LibraryPrimitive
from modulo.db.models.model_backend import ModelBackend
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.run import Run
from modulo.db.session import AsyncSessionLocal
from modulo.settings import get_settings

_log = logging.getLogger(__name__)

ConflictStrategy = Literal["skip", "overwrite", "merge"]
_EXPORT_TABLES = (
    "accounts",
    "pipelines",
    "runs",
    "audit_events",
    "library_primitives",
    "connector_instances",
    "model_backends",
)

_MODEL_MAP: dict[str, type] = {
    "accounts": Account,
    "pipelines": Pipeline,
    "runs": Run,
    "audit_events": AuditEvent,
    "library_primitives": LibraryPrimitive,
    "connector_instances": ConnectorInstance,
    "model_backends": ModelBackend,
}


# ── Auth helpers ──────────────────────────────────────────────────────────────


def _resolve_admin_auth(token: str | None) -> str | None:
    raw = token or os.environ.get("MODULO_ADMIN_SECRET", "")
    if not raw:
        return None
    if token:
        try:
            settings = get_settings()
            principal = decode_principal(raw, settings.secret_key)
            if principal.org_role not in ("admin", "owner"):
                raise click.ClickException("Token is not an admin-level JWT")
            return str(principal.user_id)
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(f"Invalid admin JWT: {exc}") from exc
    return "__admin_secret__"


async def _verify_admin_access(session: Any, org_id: uuid.UUID, admin_user_id: str) -> None:
    if admin_user_id == "__admin_secret__":
        return
    account = await get_account_by_id(session, uuid.UUID(admin_user_id))
    if account is None:
        raise click.ClickException("Admin account not found in database")
    membership = await get_membership_by_account_and_org(session, account.id, org_id)
    if membership is None:
        raise click.ClickException("Admin account does not belong to the target organisation")
    if membership.role not in ("admin", "owner"):
        raise click.ClickException("Account does not have admin-level access")


# ── Serialisation helpers ─────────────────────────────────────────────────────


def _serialise_row(row: Any) -> dict[str, Any]:
    cols = {}
    for c in row.__table__.columns:
        val = getattr(row, c.name)
        if isinstance(val, uuid.UUID):
            val = str(val)
        elif isinstance(val, datetime):
            val = val.isoformat()
        elif isinstance(val, bytes):
            val = val.hex()
        cols[c.name] = val
    return cols


def _hash_record(rec: dict[str, Any]) -> str:
    raw = json.dumps(rec, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


# ── Export helpers ──────────────────────────────────────────────────────────────


async def _collect_org_data(
    session: Any,
    org_id: uuid.UUID,
    *,
    pipelines_only: bool = False,
    users_only: bool = False,
) -> dict[str, Any]:
    bundle: dict[str, Any] = {}
    org = await get_organisation(session, org_id)
    if org is None:
        raise click.ClickException(f"Organisation {org_id} not found")
    bundle["organisation"] = _serialise_row(org)

    if pipelines_only and users_only:
        raise click.ClickException("--pipelines-only and --users-only are mutually exclusive")
    tables_to_fetch = list(_MODEL_MAP.items())
    if pipelines_only:
        tables_to_fetch = [(n, m) for n, m in tables_to_fetch if n == "pipelines"]
    elif users_only:
        tables_to_fetch = [(n, m) for n, m in tables_to_fetch if n == "accounts"]

    for name, model_cls in tqdm(tables_to_fetch, desc="Exporting tables", unit="table"):
        query = select(model_cls)
        if hasattr(model_cls, "organisation_id"):
            query = query.where(model_cls.organisation_id == org_id)
        rows = (await session.execute(query)).scalars().all()
        bundle[name] = [_serialise_row(r) for r in rows]

    bundle["exported_at"] = datetime.now(UTC).isoformat()
    return bundle


def _sort_key_id(row: dict[str, Any]) -> str:
    val = row.get("id")
    return str(val) if val is not None else ""


def _compute_export_hash(bundle: dict[str, Any]) -> str:
    hasher = hashlib.sha256()
    for table in _EXPORT_TABLES:
        for row in sorted(bundle.get(table, []), key=_sort_key_id):
            hasher.update(_hash_record(row).encode())
    return hasher.hexdigest()


def _write_jsonl(bundle: dict[str, Any], path: Path) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    export_hash = _compute_export_hash(bundle)
    hashes: dict[str, str] = {}

    with path.open("w", encoding="utf-8") as f:
        header = {
            "__meta__": {
                "version": 1,
                "exported_at": bundle.get("exported_at"),
                "export_hash": export_hash,
            }
        }
        f.write(json.dumps(header, ensure_ascii=False) + "\n")

        for table in _EXPORT_TABLES:
            rows = bundle.get(table, [])
            table_hasher = hashlib.sha256()
            for row in tqdm(rows, desc=f"Writing {table}", unit="row", leave=False):
                rec = {
                    "__table__": table,
                    "id": row.get("id"),
                    "data": row,
                    "__hash__": _hash_record(row),
                }
                table_hasher.update(rec["__hash__"].encode())
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            hashes[table] = table_hasher.hexdigest()

    hashes["__export__"] = export_hash
    return hashes


# ── Import helpers ──────────────────────────────────────────────────────────────


async def _read_jsonl(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not os.path.exists(str(path)):  # noqa: ASYNC240
        raise click.ClickException(f"Input file not found: {path}")
    meta: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        first = True
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise click.ClickException(f"Invalid JSONL line: {exc}") from exc
            if first and "__meta__" in obj:
                meta = obj["__meta__"]
                first = False
                continue
            first = False
            records.append(obj)
    return meta, records


def _group_records(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        table = rec.get("__table__")
        if table:
            groups.setdefault(table, []).append(rec)
    return groups


async def _import_org_data(
    session: Any,
    org_id: uuid.UUID,
    records: list[dict[str, Any]],
    strategy: ConflictStrategy,
    *,
    pipelines_only: bool = False,
    users_only: bool = False,
) -> dict[str, int]:
    counts: dict[str, int] = {"created": 0, "skipped": 0, "overwritten": 0, "errors": 0}
    groups = _group_records(records)
    id_map: dict[str, str] = {}

    if pipelines_only and users_only:
        raise click.ClickException("--pipelines-only and --users-only are mutually exclusive")
    tables_to_import = list(groups.items())
    if pipelines_only:
        tables_to_import = [(n, r) for n, r in tables_to_import if n == "pipelines"]
    elif users_only:
        tables_to_import = [(n, r) for n, r in tables_to_import if n == "accounts"]

    for table_name, recs in tqdm(tables_to_import, desc="Importing tables", unit="table"):
        if not table_name:
            _log.warning("Skipping record with empty __table__ key")
            counts["errors"] += 1
            continue
        model_cls = _MODEL_MAP.get(table_name)
        if model_cls is None:
            _log.warning("Skipping unknown table: %s", table_name)
            counts["errors"] += 1
            continue

        pk_cols = list(model_cls.__table__.primary_key.columns.keys())  # type: ignore[attr-defined]
        pk_col = pk_cols[0] if pk_cols else "id"
        skip_cols = {"id", pk_col, "created_at", "organisation_id"}

        for rec in tqdm(recs, desc=f"  {table_name}", unit="row", leave=False):
            raw_data = rec.get("data")
            if raw_data is None or not isinstance(raw_data, dict):
                _log.warning("Record missing or invalid 'data' key, skipping: %s", rec.get("id", "?"))
                counts["errors"] += 1
                continue
            row_data = dict(raw_data)
            row_data.pop("organisation_id", None)
            row_id = row_data.get("id")
            old_id_str = str(row_id) if row_id else None
            _remap_fk_row(row_data, id_map)

            try:
                existing = await _find_existing_row(session, model_cls, pk_col, row_id, org_id)

                if existing is not None and strategy == "skip":
                    if old_id_str and hasattr(existing, pk_col):
                        id_map[old_id_str] = str(getattr(existing, pk_col))
                    counts["skipped"] += 1
                    continue

                async with session.begin_nested():
                    if existing is not None and strategy in ("overwrite", "merge"):
                        for col, val in row_data.items():
                            if col in skip_cols or not hasattr(existing, col):
                                continue
                            if strategy == "merge":
                                current = getattr(existing, col)
                                if current is not None:
                                    continue
                            setattr(existing, col, val)
                        if old_id_str and hasattr(existing, pk_col):
                            id_map[old_id_str] = str(getattr(existing, pk_col))
                        counts["overwritten"] += 1
                        continue

                    if existing is None:
                        row_data.pop("id", None)
                        if hasattr(model_cls, "organisation_id"):
                            row_data["organisation_id"] = org_id
                        new_obj = model_cls(**row_data)
                        session.add(new_obj)
                        await session.flush()
                        if old_id_str and hasattr(new_obj, pk_col):
                            id_map[old_id_str] = str(getattr(new_obj, pk_col))
                        counts["created"] += 1

            except Exception as exc:
                _log.warning("Error importing %s row %s: %s", table_name, row_id or "?", exc)
                counts["errors"] += 1

        try:
            await session.flush()
        except Exception as exc:
            _log.error("Flush failed after importing table %s: %s", table_name, exc)
            raise

    return counts


def _remap_fk_row(row_data: dict[str, Any], id_map: dict[str, str]) -> None:
    for key, val in list(row_data.items()):
        if key in ("id", "organisation_id", "created_at", "updated_at"):
            continue
        if val is not None:
            mapped = id_map.get(str(val))
            if mapped is not None:
                row_data[key] = uuid.UUID(mapped)


async def _find_existing_row(
    session: Any,
    model_cls: type,
    pk_col: str,
    row_id: str | None,
    org_id: uuid.UUID,
) -> Any:
    if not row_id:
        return None
    stmt: Any = select(model_cls).where(getattr(model_cls, pk_col) == uuid.UUID(row_id))
    if hasattr(model_cls, "organisation_id"):
        stmt = stmt.where(model_cls.organisation_id == org_id)
    return (await session.execute(stmt)).scalar_one_or_none()


# ── Verify helpers ──────────────────────────────────────────────────────────────


def _verify_export(meta: dict[str, Any], records: list[dict[str, Any]]) -> bool:
    expected_hash = meta.get("export_hash", "")
    groups = _group_records(records)

    combined = hashlib.sha256()
    for table in _EXPORT_TABLES:
        table_records = groups.get(table, [])
        table_hasher = hashlib.sha256()
        for rec in sorted(table_records, key=_sort_key_id):
            row_hash = rec.get("__hash__", "")
            table_hasher.update(row_hash.encode())
            combined.update(row_hash.encode())
        computed = table_hasher.hexdigest()
        click.echo(f"  {table:22s}  {computed[:16]}...")
    computed_export_hash = combined.hexdigest()

    if computed_export_hash == expected_hash:
        click.echo(f"\nExport hash: {computed_export_hash}  OK")
        return True

    click.echo(f"\nExport hash: computed={computed_export_hash}  expected={expected_hash}")
    click.echo("Export verification FAILED — data integrity issue detected.")
    return False


# ── CLI commands ──────────────────────────────────────────────────────────────


@click.group()
@click.option(
    "--token",
    envvar="MODULO_ADMIN_TOKEN",
    default=None,
    help="Admin JWT (or set MODULO_ADMIN_TOKEN)",
)
@click.pass_context
def cli(ctx: click.Context, token: str | None) -> None:
    """Modulo migration tool — export, import, and verify org data."""
    ctx.ensure_object(dict)
    admin_id = _resolve_admin_auth(token)
    if admin_id is None:
        raise click.ClickException(
            "Admin authentication required. Provide --token / MODULO_ADMIN_TOKEN "
            "or set MODULO_ADMIN_SECRET environment variable."
        )
    ctx.obj["admin_user_id"] = admin_id


def _parse_uuid(raw: str, label: str = "UUID") -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError) as exc:
        raise click.ClickException(f"Invalid {label}: {raw!r}") from exc


@cli.command()
@click.argument("org_id", type=str)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default="export.jsonl",
    help="Output JSONL path",
)
@click.option("--pipelines-only", is_flag=True, default=False, help="Export only pipelines")
@click.option("--users-only", is_flag=True, default=False, help="Export only users")
@click.pass_context
def export_org(ctx: click.Context, org_id: str, output: Path, pipelines_only: bool, users_only: bool) -> None:
    """Export all organisation data as a JSONL bundle."""
    asyncio.run(_async_export_org(ctx, _parse_uuid(org_id, "organisation ID"), output, pipelines_only, users_only))


async def _async_export_org(
    ctx: click.Context,
    org_id: uuid.UUID,
    output: Path,
    pipelines_only: bool,
    users_only: bool,
) -> None:
    try:
        async with AsyncSessionLocal() as session:
            await _verify_admin_access(session, org_id, ctx.obj["admin_user_id"])
            bundle = await _collect_org_data(session, org_id, pipelines_only=pipelines_only, users_only=users_only)
            hashes = _write_jsonl(bundle, output)
            record_count = sum(len(v) for k, v in bundle.items() if isinstance(v, list))
            click.echo(f"Exported {record_count} records to {output}")
            click.echo(f"Export hash: {hashes['__export__']}")
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f"Export failed: {exc}") from exc


@cli.command()
@click.argument("org_id", type=str)
@click.option(
    "--input",
    "-i",
    "input_path",
    type=click.Path(path_type=Path, exists=True),
    required=True,
    help="Input JSONL path",
)
@click.option(
    "--on-conflict",
    type=click.Choice(["skip", "overwrite", "merge"]),
    default="skip",
    help="Conflict resolution strategy",
)
@click.option("--pipelines-only", is_flag=True, default=False, help="Import only pipelines")
@click.option("--users-only", is_flag=True, default=False, help="Import only users")
@click.pass_context
def import_org(
    ctx: click.Context,
    org_id: str,
    input_path: Path,
    on_conflict: ConflictStrategy,
    pipelines_only: bool,
    users_only: bool,
) -> None:
    """Import organisation data from a JSONL bundle with conflict resolution."""
    parsed_org_id = _parse_uuid(org_id, "organisation ID")
    asyncio.run(
        _async_import_org(ctx, parsed_org_id, input_path, on_conflict, pipelines_only, users_only)
    )


async def _async_import_org(
    ctx: click.Context,
    org_id: uuid.UUID,
    input_path: Path,
    strategy: ConflictStrategy,
    pipelines_only: bool,
    users_only: bool,
) -> None:
    try:
        async with AsyncSessionLocal() as session:
            await _verify_admin_access(session, org_id, ctx.obj["admin_user_id"])
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f"Auth verification failed: {exc}") from exc

    _meta, records = await _read_jsonl(input_path)
    click.echo(f"Loaded {len(records)} records from {input_path}")

    if _meta.get("export_hash") and not _verify_export(_meta, records):
        raise click.ClickException(
            "Import aborted: hash verification failed — file may be corrupted"
        )

    try:
        async with AsyncSessionLocal() as session:
            counts = await _import_org_data(
                session,
                org_id,
                records,
                strategy,
                pipelines_only=pipelines_only,
                users_only=users_only,
            )
            await session.commit()
            click.echo(
                f"Import complete: {counts['created']} created, "
                f"{counts['overwritten']} overwritten, "
                f"{counts['skipped']} skipped, "
                f"{counts['errors']} errors"
            )
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f"Import failed: {exc}") from exc


@cli.command()
@click.argument("org_id", type=str)
@click.option(
    "--input",
    "-i",
    "input_path",
    type=click.Path(path_type=Path, exists=True),
    required=True,
    help="Input JSONL path",
)
@click.pass_context
def verify_export(ctx: click.Context, org_id: str, input_path: Path) -> None:
    """Verify export integrity by re-computing hashes."""
    asyncio.run(_async_verify_export(ctx, _parse_uuid(org_id, "organisation ID"), input_path))


async def _async_verify_export(ctx: click.Context, org_id: uuid.UUID, input_path: Path) -> None:
    try:
        meta, records = await _read_jsonl(input_path)
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f"Failed to read export file: {exc}") from exc
    ok = _verify_export(meta, records)
    if not ok:
        raise click.ClickException("Verification failed — data integrity issue detected")


if __name__ == "__main__":
    cli()
