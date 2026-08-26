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
from dataclasses import dataclass
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

# Label used when reporting a malformed --org-id argument to _parse_uuid.
_ORG_ID_ARG_LABEL = "organisation ID"

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

# Fetch rows in bounded batches so exports stay memory-safe for large orgs
# (same page size as migrate_org.py's proven paginated export pattern).
_PAGE_SIZE = 500

# Upper bound on any single database operation during export/import so a slow
# or hung database fails loudly instead of blocking the CLI indefinitely.
_DB_OP_TIMEOUT_SECONDS = 600


# ── Auth helpers ──────────────────────────────────────────────────────────────


def _resolve_admin_auth(token: str | None) -> str | None:
    raw = token or os.environ.get("MODULO_ADMIN_SECRET", "")
    if not raw:
        return None
    if token:
        try:
            settings = get_settings()
            principal = decode_principal(raw, settings.secret_key)
            if principal.org_role != "admin":
                raise click.ClickException("Token is not an admin-level JWT")
            return str(principal.user_id)
        except asyncio.CancelledError:
            raise
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
    if membership.role != "admin":
        raise click.ClickException("Account does not have admin-level access")


# ── Shared helpers ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ScopeFlags:
    """Pair of mutually-exclusive table-scoping flags shared by import/export."""

    pipelines_only: bool = False
    users_only: bool = False


def _filter_scope(tables: list[tuple[str, Any]], *, pipelines_only: bool, users_only: bool) -> list[tuple[str, Any]]:
    """Narrow a table set to pipelines or accounts when a scoping flag is set."""
    if pipelines_only and users_only:
        raise click.ClickException("--pipelines-only and --users-only are mutually exclusive")
    if pipelines_only:
        return [(name, item) for name, item in tables if name == "pipelines"]
    if users_only:
        return [(name, item) for name, item in tables if name == "accounts"]
    return list(tables)


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


async def _fetch_table_rows(session: Any, model_cls: Any, org_id: uuid.UUID) -> list[dict[str, Any]]:
    """Fetch every row of one table in bounded pages, scoped to the org."""
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        query: Any = select(model_cls).order_by(model_cls.id)
        if hasattr(model_cls, "organisation_id"):
            query = query.where(model_cls.organisation_id == org_id)
        query = query.offset(offset).limit(_PAGE_SIZE)
        batch = (await session.execute(query)).scalars().all()
        if not batch:
            break
        rows.extend(_serialise_row(r) for r in batch)
        offset += _PAGE_SIZE
    return rows


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

    tables_to_fetch = _filter_scope(list(_MODEL_MAP.items()), pipelines_only=pipelines_only, users_only=users_only)
    for name, model_cls in tqdm(tables_to_fetch, desc="Exporting tables", unit="table"):
        bundle[name] = await _fetch_table_rows(session, model_cls, org_id)

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


def _read_jsonl_sync(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
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


async def _read_jsonl(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not await asyncio.to_thread(path.exists):
        raise click.ClickException(f"Input file not found: {path}")
    return await asyncio.to_thread(_read_jsonl_sync, path)


def _group_records(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        table = rec.get("__table__")
        if table:
            groups.setdefault(table, []).append(rec)
    return groups


@dataclass
class _TableImportConfig:
    """Per-table settings shared by every row import within one table."""

    session: Any
    model_cls: type
    pk_col: str
    skip_cols: set[str]
    strategy: ConflictStrategy
    org_id: uuid.UUID
    table_name: str


def _build_table_config(
    session: Any,
    model_cls: type,
    strategy: ConflictStrategy,
    org_id: uuid.UUID,
    table_name: str,
) -> _TableImportConfig:
    pk_cols = list(model_cls.__table__.primary_key.columns.keys())  # type: ignore[attr-defined]
    pk_col = pk_cols[0] if pk_cols else "id"
    skip_cols = {"id", pk_col, "created_at", "organisation_id"}
    return _TableImportConfig(
        session=session,
        model_cls=model_cls,
        pk_col=pk_col,
        skip_cols=skip_cols,
        strategy=strategy,
        org_id=org_id,
        table_name=table_name,
    )


def _map_existing_id(old_id_str: str | None, obj: Any, pk_col: str, id_map: dict[str, str]) -> None:
    """Record the remap from an exported row id to the stored primary key value."""
    if old_id_str and hasattr(obj, pk_col):
        id_map[old_id_str] = str(getattr(obj, pk_col))


def _apply_conflict_strategy(existing: Any, row_data: dict[str, Any], cfg: _TableImportConfig) -> None:
    """Copy row values onto an existing row according to the conflict strategy."""
    for col, val in row_data.items():
        if col in cfg.skip_cols or not hasattr(existing, col):
            continue
        if cfg.strategy == "merge":
            current = getattr(existing, col)
            if current is not None:
                continue
        setattr(existing, col, val)


async def _create_row(
    cfg: _TableImportConfig,
    row_data: dict[str, Any],
    old_id_str: str | None,
    id_map: dict[str, str],
) -> None:
    """Insert a new row, stripping the exported id so the DB assigns a fresh one."""
    row_data.pop("id", None)
    if hasattr(cfg.model_cls, "organisation_id"):
        row_data["organisation_id"] = cfg.org_id
    new_obj = cfg.model_cls(**row_data)
    cfg.session.add(new_obj)
    await cfg.session.flush()
    _map_existing_id(old_id_str, new_obj, cfg.pk_col, id_map)


async def _import_row(
    cfg: _TableImportConfig,
    rec: dict[str, Any],
    id_map: dict[str, str],
    counts: dict[str, int],
) -> None:
    """Import a single export record under the configured conflict strategy."""
    raw_data = rec.get("data")
    if raw_data is None or not isinstance(raw_data, dict):
        _log.warning("Record missing or invalid 'data' key, skipping: %s", rec.get("id", "?"))
        counts["errors"] += 1
        return
    row_data = dict(raw_data)
    row_data.pop("organisation_id", None)
    row_id = row_data.get("id")
    old_id_str = str(row_id) if row_id else None
    _remap_fk_row(row_data, id_map)

    try:
        existing = await _find_existing_row(cfg.session, cfg.model_cls, cfg.pk_col, row_id, cfg.org_id)

        if existing is not None and cfg.strategy == "skip":
            _map_existing_id(old_id_str, existing, cfg.pk_col, id_map)
            counts["skipped"] += 1
            return

        async with cfg.session.begin_nested():
            if existing is not None and cfg.strategy in ("overwrite", "merge"):
                _apply_conflict_strategy(existing, row_data, cfg)
                _map_existing_id(old_id_str, existing, cfg.pk_col, id_map)
                counts["overwritten"] += 1
                return

            if existing is None:
                await _create_row(cfg, row_data, old_id_str, id_map)
                counts["created"] += 1

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log.exception("Error importing %s row %s: %s", cfg.table_name, row_id or "?", exc)
        counts["errors"] += 1


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

    tables_to_import = _filter_scope(list(groups.items()), pipelines_only=pipelines_only, users_only=users_only)

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

        cfg = _build_table_config(session, model_cls, strategy, org_id, table_name)
        for rec in tqdm(recs, desc=f"  {table_name}", unit="row", leave=False):
            await _import_row(cfg, rec, id_map, counts)

        try:
            await session.flush()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.exception("Flush failed after importing table %s: %s", table_name, exc)
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
    scope = _ScopeFlags(pipelines_only=pipelines_only, users_only=users_only)
    asyncio.run(_async_export_org(ctx, _parse_uuid(org_id, _ORG_ID_ARG_LABEL), output, scope))


async def _async_export_org(ctx: click.Context, org_id: uuid.UUID, output: Path, scope: _ScopeFlags) -> None:
    export_completed = False
    try:
        async with asyncio.timeout(_DB_OP_TIMEOUT_SECONDS):
            async with AsyncSessionLocal() as session:
                await _verify_admin_access(session, org_id, ctx.obj["admin_user_id"])
                bundle = await _collect_org_data(
                    session, org_id, pipelines_only=scope.pipelines_only, users_only=scope.users_only
                )
                hashes = _write_jsonl(bundle, output)
                export_completed = True
                record_count = sum(len(v) for v in bundle.values() if isinstance(v, list))
                click.echo(f"Exported {record_count} records to {output}")
                click.echo(f"Export hash: {hashes['__export__']}")
    except asyncio.CancelledError:
        raise
    except click.ClickException:
        raise
    except TimeoutError:
        raise click.ClickException(
            f"Export timed out after {_DB_OP_TIMEOUT_SECONDS}s — database may be slow or hung"
        ) from None
    except Exception as exc:
        raise click.ClickException(f"Export failed: {exc}") from exc
    finally:
        if not export_completed and await asyncio.to_thread(output.exists):
            await asyncio.to_thread(output.unlink, missing_ok=True)


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
    parsed_org_id = _parse_uuid(org_id, _ORG_ID_ARG_LABEL)
    scope = _ScopeFlags(pipelines_only=pipelines_only, users_only=users_only)
    asyncio.run(_async_import_org(ctx, parsed_org_id, input_path, on_conflict, scope))


async def _async_import_org(
    ctx: click.Context,
    org_id: uuid.UUID,
    input_path: Path,
    strategy: ConflictStrategy,
    scope: _ScopeFlags,
) -> None:
    meta, records = await _read_jsonl(input_path)
    click.echo(f"Loaded {len(records)} records from {input_path}")

    if meta.get("export_hash") and not _verify_export(meta, records):
        raise click.ClickException("Import aborted: hash verification failed — file may be corrupted")

    try:
        async with asyncio.timeout(_DB_OP_TIMEOUT_SECONDS):
            async with AsyncSessionLocal() as session:
                await _verify_admin_access(session, org_id, ctx.obj["admin_user_id"])
                counts = await _import_org_data(
                    session,
                    org_id,
                    records,
                    strategy,
                    pipelines_only=scope.pipelines_only,
                    users_only=scope.users_only,
                )
                await session.commit()
                click.echo(
                    f"Import complete: {counts['created']} created, "
                    f"{counts['overwritten']} overwritten, "
                    f"{counts['skipped']} skipped, "
                    f"{counts['errors']} errors"
                )
    except asyncio.CancelledError:
        raise
    except click.ClickException:
        raise
    except TimeoutError:
        raise click.ClickException(
            f"Import timed out after {_DB_OP_TIMEOUT_SECONDS}s — database may be slow or hung"
        ) from None
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
    asyncio.run(_async_verify_export(ctx, _parse_uuid(org_id, _ORG_ID_ARG_LABEL), input_path))


async def _async_verify_export(_ctx: click.Context, org_id: uuid.UUID, input_path: Path) -> None:
    try:
        meta, records = await _read_jsonl(input_path)
    except asyncio.CancelledError:
        raise
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(f"Failed to read export file: {exc}") from exc
    ok = _verify_export(meta, records)
    if not ok:
        raise click.ClickException("Verification failed — data integrity issue detected")


if __name__ == "__main__":
    cli()
