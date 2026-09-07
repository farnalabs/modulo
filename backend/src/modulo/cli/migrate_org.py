"""modulo export-org / import-org: Self-hosted to SaaS migration CLI.

Usage:
  modulo export-org --org-id <uuid> --output <file.json>
  modulo import-org --input <file.json> --org-id <uuid> --conflict <skip|overwrite|rename>
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from tqdm import tqdm  # type: ignore[import-untyped]

from modulo.db.models import (
    Account,
    Agent,
    ConnectorInstance,
    LibraryPrimitive,
    ModelBackend,
    Organisation,
    Pipeline,
    Run,
    Schema,
    SchemaVersion,
    Team,
)
from modulo.db.session import AsyncSessionLocal

_log = logging.getLogger(__name__)

EXPORT_VERSION = 1
PAGE_SIZE = 500

ConflictStrategy = Literal["skip", "overwrite", "rename"]

ENTITY_ORDER: list[tuple[str, type]] = [
    ("users", Account),
    ("teams", Team),
    ("schemas", Schema),
    ("schema_versions", SchemaVersion),
    ("model_backends", ModelBackend),
    ("library_primitives", LibraryPrimitive),
    ("connector_instances", ConnectorInstance),
    ("agents", Agent),
    ("pipelines", Pipeline),
    ("runs", Run),
]

NAME_CONFLICT_FIELD: dict[str, str] = {
    "users": "email",
    "teams": "name",
    "schemas": "name",
    "model_backends": "name",
    "library_primitives": "name",
    "connector_instances": "name",
    "agents": "name",
    "pipelines": "name",
}

FK_COLUMNS: dict[str, list[str]] = {
    "users": ["organisation_id"],
    "teams": ["organisation_id", "created_by"],
    "schemas": ["organisation_id", "created_by"],
    "schema_versions": ["organisation_id", "schema_id", "created_by"],
    "model_backends": ["organisation_id", "owner_team_id", "created_by"],
    "library_primitives": ["organisation_id", "forked_from", "owner_team_id", "created_by"],
    "connector_instances": ["organisation_id", "owner_id", "owner_team_id"],
    "agents": [
        "organisation_id",
        "model_backend_id",
        "library_id",
        "created_by",
        "input_schema_id",
        "output_schema_id",
    ],
    "pipelines": ["organisation_id", "owner_team_id", "created_by"],
    "runs": ["organisation_id", "pipeline_id", "snapshot_id", "trigger_id", "owner_team_id", "created_by"],
}

IMPORT_SKIP_COLS: dict[str, set[str]] = {
    "users": {"id", "organisation_id", "created_at", "updated_at", "last_login"},
    "teams": {"id", "organisation_id", "created_at", "updated_at"},
    "schemas": {"id", "organisation_id", "created_at", "updated_at"},
    "schema_versions": {"id", "organisation_id", "created_at", "updated_at"},
    "model_backends": {"id", "organisation_id", "created_at", "updated_at"},
    "library_primitives": {"id", "organisation_id", "created_at", "updated_at"},
    "connector_instances": {"id", "organisation_id", "created_at", "updated_at"},
    "agents": {"id", "organisation_id", "created_at", "updated_at"},
    "pipelines": {"id", "organisation_id", "created_at", "updated_at"},
    "runs": {"id", "organisation_id", "created_at", "updated_at"},
}


# ── Serialisation ──────────────────────────────────────────────────────────────


def _serialise(val: Any) -> Any:
    if isinstance(val, uuid.UUID):
        return str(val)
    if isinstance(val, datetime):
        return val.isoformat()
    if isinstance(val, bytes):
        return val.hex()
    if isinstance(val, Decimal):
        return str(val)
    if isinstance(val, set):
        return list(val)
    return val


def _serialise_row(row: Any) -> dict[str, Any]:
    cols: dict[str, Any] = {}
    for c in row.__table__.columns:
        val = getattr(row, c.name)
        cols[c.name] = _serialise(val) if val is not None else None
    return cols


# ── Hash verification ──────────────────────────────────────────────────────────


def _compute_hash(bundle: dict[str, Any]) -> str:
    meta = bundle.get("__meta__", {})
    clean_meta = {k: v for k, v in meta.items() if k != "hash"}
    payload = {k: v for k, v in bundle.items() if k != "__meta__"}
    payload["__meta__"] = clean_meta
    serialised = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=_serialise)
    return hashlib.sha256(serialised.encode()).hexdigest()


def _verify_hash(bundle: dict[str, Any]) -> bool:
    expected = bundle.get("__meta__", {}).get("hash", "")
    computed = _compute_hash(bundle)
    if computed != expected:
        tqdm.write(f"HASH MISMATCH: computed={computed}, expected={expected}")
        return False
    return True


def _validate_bundle(bundle: Any) -> list[str]:
    """Structural validation for an import bundle.

    Returns a list of human-readable problems; an empty list means the bundle
    is structurally well-formed. The hash check (``_verify_hash``) detects
    tampering, but says nothing about shape — a hand-edited file can carry a
    recomputed hash while its tables/rows are malformed. Validating before
    import means corrupt files are rejected up front with a clear message
    instead of being accepted and failing unpredictably mid-import (iterating a
    dict's keys as rows, crashing on a non-object root, etc.).
    """
    errors: list[str] = []

    if not isinstance(bundle, dict):
        return [f"Import bundle must be a JSON object, got {type(bundle).__name__}"]

    if not isinstance(bundle.get("__meta__"), dict):
        errors.append("Bundle missing '__meta__' object (export metadata)")

    if not isinstance(bundle.get("organisation"), dict):
        errors.append("Bundle 'organisation' must be a JSON object")

    for table_name, _model_cls in ENTITY_ORDER:
        rows = bundle.get(table_name)
        if rows is None:
            continue
        if not isinstance(rows, list):
            errors.append(f"Bundle '{table_name}' must be a JSON array, got {type(rows).__name__}")
            continue
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append(f"Bundle '{table_name}' row {idx} must be a JSON object, got {type(row).__name__}")
                continue
            rid = row.get("id")
            if rid is not None and not isinstance(rid, str):
                errors.append(f"Bundle '{table_name}' row {idx} 'id' must be a string, got {type(rid).__name__}")

    return errors


# ── Export ─────────────────────────────────────────────────────────────────────


async def _export_entity(
    session: Any,
    model_cls: Any,
    org_id: uuid.UUID,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0

    while True:
        stmt: Any = select(model_cls)
        # Not every entity is org-scoped (e.g. Account — users belong to orgs
        # via OrgMembership), mirroring the guard used by the click-based CLI.
        if hasattr(model_cls, "organisation_id"):
            stmt = stmt.where(model_cls.organisation_id == org_id)
        stmt = stmt.order_by(model_cls.id).offset(offset).limit(PAGE_SIZE)
        batch = (await session.execute(stmt)).scalars().all()
        if not batch:
            break
        rows.extend(_serialise_row(row) for row in batch)
        offset += PAGE_SIZE

    return rows


async def _export_organisation(session: Any, org_id: uuid.UUID) -> dict[str, Any]:
    org = await session.get(Organisation, org_id)
    if org is None:
        msg = f"Organisation {org_id} not found"
        raise SystemExit(msg)
    return _serialise_row(org)


async def _do_export(org_id: uuid.UUID, _output: Path) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "__meta__": {
            "version": EXPORT_VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "org_id": str(org_id),
        },
    }

    try:
        async with AsyncSessionLocal() as session:
            org = await _export_organisation(session, org_id)
            bundle["organisation"] = org
            bundle["__meta__"]["org_name"] = org.get("name", "")

            for table_name, model_cls in tqdm(ENTITY_ORDER, desc="Exporting tables", unit="table"):
                rows = await _export_entity(session, model_cls, org_id)
                bundle[table_name] = rows
                tqdm.write(f"  {table_name:22s}  {len(rows):>6d} rows")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        msg = f"Database connection failed during export: {exc}"
        raise SystemExit(msg) from exc

    bundle["__meta__"]["hash"] = _compute_hash(bundle)
    return bundle


def _write_bundle(bundle: dict[str, Any], path: Path, *, force: bool = False) -> None:
    if path.exists() and not force:
        msg = f"Output file already exists: {path}. Use --force to overwrite."
        raise SystemExit(msg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(bundle, f, ensure_ascii=False, indent=2, default=_serialise)
    except OSError as exc:
        msg = f"Failed to write export to {path}: {exc}"
        raise SystemExit(msg) from exc


# ── Import ────────────────────────────────────────────────────────────────────


def _load_bundle(path: Path) -> dict[str, Any]:
    if not path.exists():
        msg = f"Import file not found: {path}"
        raise SystemExit(msg)
    try:
        with path.open("r", encoding="utf-8") as f:
            bundle: dict[str, Any] = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"Failed to read import file {path}: {exc}"
        raise SystemExit(msg) from exc
    errors = _validate_bundle(bundle)
    if errors:
        details = "\n  - ".join(errors)
        msg = f"Import aborted: invalid bundle structure:\n  - {details}"
        raise SystemExit(msg)
    if not _verify_hash(bundle):
        msg = "Import aborted: file hash verification failed"
        raise SystemExit(msg)
    return bundle


def _remap_fk(
    row: dict[str, Any],
    table_name: str,
    id_map: dict[str, str],
) -> dict[str, Any]:
    remapped = dict(row)
    fk_cols = FK_COLUMNS.get(table_name, [])
    for col in fk_cols:
        val = remapped.get(col)
        if val is not None:
            mapped = id_map.get(str(val))
            if mapped is not None:
                remapped[col] = uuid.UUID(mapped)
    return remapped


async def _do_import(
    bundle: dict[str, Any],
    org_id: uuid.UUID,
    strategy: ConflictStrategy,
) -> dict[str, int]:
    counts: dict[str, int] = {"created": 0, "overwritten": 0, "skipped": 0, "errors": 0}
    id_map: dict[str, str] = {}

    try:
        async with AsyncSessionLocal() as session:
            for table_name, model_cls in tqdm(ENTITY_ORDER, desc="Importing tables", unit="table"):
                rows: list[dict[str, Any]] = bundle.get(table_name, [])
                name_field = NAME_CONFLICT_FIELD.get(table_name)
                skip_cols = IMPORT_SKIP_COLS.get(table_name, {"id", "organisation_id", "created_at", "updated_at"})

                for row in tqdm(rows, desc=f"  {table_name}", unit="row", leave=False):
                    try:
                        old_id = row.get("id")
                        old_id_str = str(old_id) if old_id is not None else None
                        name_val: str | None = row.get(name_field) if name_field else None

                        existing = None
                        if name_val and name_field:
                            stmt: Any = select(model_cls)
                            if hasattr(model_cls, "organisation_id"):
                                stmt = stmt.where(model_cls.organisation_id == org_id)
                            stmt = stmt.where(getattr(model_cls, name_field) == name_val)
                            existing = (await session.execute(stmt)).scalars().first()

                        if existing is not None:
                            if strategy == "skip":
                                if old_id_str:
                                    id_map[old_id_str] = str(existing.id)
                                counts["skipped"] += 1
                                continue

                            if strategy == "rename" and name_val and name_field:
                                max_attempts = 10000
                                base = f"{name_val}_imported"
                                new_name = base
                                counter = 2
                                while counter <= max_attempts:
                                    chk_stmt: Any = select(model_cls)
                                    if hasattr(model_cls, "organisation_id"):
                                        chk_stmt = chk_stmt.where(model_cls.organisation_id == org_id)
                                    chk_stmt = chk_stmt.where(getattr(model_cls, name_field) == new_name)
                                    chk = (await session.execute(chk_stmt)).scalars().first()
                                    if chk is None:
                                        break
                                    new_name = f"{base}_{counter}"
                                    counter += 1
                                else:
                                    raise SystemExit(
                                        f"Could not find available name for '{name_val}' after {max_attempts} attempts"
                                    )
                                row[name_field] = new_name
                                existing = None

                        row_data = _remap_fk(row, table_name, id_map)
                        row_data.pop("organisation_id", None)
                        row_data.pop("id", None)
                        for col in skip_cols:
                            row_data.pop(col, None)

                        async with session.begin_nested():
                            if existing is not None and strategy == "overwrite":
                                for col, val in row_data.items():
                                    if hasattr(existing, col):
                                        setattr(existing, col, val)
                                if old_id_str:
                                    id_map[old_id_str] = str(existing.id)
                                counts["overwritten"] += 1
                            else:
                                if hasattr(model_cls, "organisation_id"):
                                    row_data["organisation_id"] = org_id
                                obj = model_cls(**row_data)
                                session.add(obj)
                                await session.flush()
                                if old_id_str:
                                    id_map[old_id_str] = str(obj.id)
                                counts["created"] += 1

                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        rid = row.get("id", "?")
                        _log.exception("Error importing %s row %s", table_name, rid)
                        tqdm.write(f"  ERROR importing {table_name} row {rid}: {exc}")
                        counts["errors"] += 1

            await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        msg = f"Database connection failed during import: {exc}"
        raise SystemExit(msg) from exc

    return counts


# ── CLI ────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="modulo",
        description="Modulo migration CLI — export and import organisation data.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    export_p = sub.add_parser("export-org", help="Export all org data as a JSON bundle")
    export_p.add_argument("--org-id", required=True, help="Organisation UUID")
    export_p.add_argument("--output", "-o", type=Path, default="export.json", help="Output JSON path")
    export_p.add_argument("--force", action="store_true", help="Overwrite existing output file")
    export_p.set_defaults(func=cmd_export)

    import_p = sub.add_parser("import-org", help="Import org data from a JSON bundle")
    import_p.add_argument("--input", "-i", type=Path, required=True, help="Input JSON path")
    import_p.add_argument("--org-id", required=True, help="Target organisation UUID")
    import_p.add_argument(
        "--conflict",
        choices=["skip", "overwrite", "rename"],
        default="skip",
        help="Conflict resolution strategy for existing entities",
    )
    import_p.set_defaults(func=cmd_import)

    return parser


def _parse_uuid(raw: str, label: str = "UUID") -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError) as exc:
        msg = f"Invalid {label}: {raw!r}"
        raise SystemExit(msg) from exc


def cmd_export(args: argparse.Namespace) -> None:
    org_id = _parse_uuid(args.org_id, "organisation ID")
    output: Path = args.output
    force: bool = getattr(args, "force", False)
    bundle = asyncio.run(_do_export(org_id, output))
    _write_bundle(bundle, output, force=force)


def cmd_import(args: argparse.Namespace) -> None:
    org_id = _parse_uuid(args.org_id, "organisation ID")
    input_path: Path = args.input
    strategy: ConflictStrategy = args.conflict

    bundle = _load_bundle(input_path)

    asyncio.run(_do_import(bundle, org_id, strategy))


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
