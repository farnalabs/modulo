"""One-off backfill: repair legacy lifecycle-map content blocked by FAR-175 validation.

FAR-175 tightened ``normalize_content`` (``modulo.core.lifecycle_map.validation``)
to reject dangling edges and duplicate stage/edge ids with a 422. Maps whose
``content_json`` predates that validation now fail EVERY content edit --
including graduation -- because ``save_map_version`` and ``graduate_stage``
re-validate the stored graph before writing.

This tool repairs such legacy content in place using the pure cleaner
``clean_legacy_content`` (dedupes stage/edge ids, drops dangling edges, and
greedily breaks transition cycles). When the content changed AND the cleaned
result passes ``normalize_content``, it rewrites ``content_json`` and rebuilds
the ``lifecycle_map_stages`` junction projection from the cleaned graph, so the
map becomes editable again immediately.

Dry-run by default. ``--apply`` commits per map in batches. Any map the cleaner
cannot repair (non-dict ``content_json``, ``stages``/``edges`` not arrays,
cleaned content still failing validation) is left untouched, reported, and
makes the tool exit 1 so an operator inspects it by hand. A nonzero exit means
some legacy maps are still blocked -- never report success while a map is left
behind.

Usage::

    DATABASE_URL=postgresql+asyncpg://... python backfill_lifecycle_map_legacy_content.py
    DATABASE_URL=... python backfill_lifecycle_map_legacy_content.py --apply
    DATABASE_URL=... python backfill_lifecycle_map_legacy_content.py --apply --limit 100

This is a version-controlled maintenance tool (see ``repair_accounts_fks.py``
and ``backfill_node_telemetry.py``); it uses a SYNC SQLAlchemy engine
(psycopg) and is never invoked from the async application path. Quiesce writes
(app drained / workers stopped) while running ``--apply`` so the per-map
read-modify-write is not raced by live edits.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any, TextIO, cast

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from psycopg.types.json import Jsonb
from sqlalchemy import create_engine, text

from modulo.core.lifecycle_map.validation import (
    LifecycleMapContentError,
    clean_legacy_content,
    normalize_content,
)

_log = logging.getLogger("backfill_lifecycle_map_legacy_content")

_UPDATE_SQL = text("UPDATE lifecycle_maps SET content_json = :content WHERE id = :map_id")
_DELETE_JUNCTION_SQL = text("DELETE FROM lifecycle_map_stages WHERE map_id = :map_id")
_INSERT_JUNCTION_SQL = text(
    "INSERT INTO lifecycle_map_stages "
    "(id, map_id, version, stage_id, stage_name, position, stage_type, pipeline_id, "
    " organisation_id, account_id) "
    "VALUES (:id, :map_id, :version, :stage_id, :stage_name, :position, :stage_type, "
    " :pipeline_id, :organisation_id, :account_id)"
)


def _bind_json_param(value: Any, dialect_name: str) -> Any:
    """Adapt a JSON dict for a bound parameter on the target dialect.

    psycopg3 (Postgres) cannot adapt a raw ``dict`` to ``%s`` -- wrap it in
    ``psycopg.types.json.Jsonb`` so the driver serialises it as jsonb. SQLite
    (used only by tests) cannot bind a dict either; its JSON columns store the
    serialised TEXT, so dump to a JSON string there. Other dialects bind dicts
    natively and are passed through.
    """
    if value is None:
        return None
    if dialect_name == "postgresql":
        return Jsonb(value)
    if dialect_name == "sqlite":
        return json.dumps(value)
    return value


def _resolve_db_url(raw: str) -> str:
    """Convert a SQLAlchemy/async-style URL into a sync psycopg connection string."""
    url = raw
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql+psycopg://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix) :]
            break
    return url


def _load_content(raw: Any) -> Any:
    """Decode a stored ``content_json`` value read through a raw ``text()`` scan.

    On Postgres (psycopg) a JSON/JSONB column comes back as a Python dict, but
    on SQLite (tests) the same column stores serialised TEXT and the raw scan
    does not run the ORM JSON decoder -- so it must be decoded here.
    """
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw
    return raw


def _clean_map(content_json: Any) -> tuple[dict[str, Any] | None, list[str], bool, str | None]:
    """Repair one map's content_json for persisting.

    Returns ``(cleaned, changes, ok, reason)``. When ``ok`` is False the map
    must be left untouched (non-dict content, or cleaned content that still
    fails ``normalize_content``). When ``changes`` is empty the map is already
    valid and needs no write.
    """
    if not isinstance(content_json, dict):
        return None, [], False, "content_json is not an object"
    cleaned, changes = clean_legacy_content(content_json)
    if not changes:
        return cleaned, [], True, None
    try:
        normalize_content(cleaned)
    except LifecycleMapContentError as exc:
        return None, changes, False, f"cleaned content still invalid: {exc}"
    return cleaned, changes, True, None


def _reproject_junction(conn: Any, row: Any, content: dict[str, Any], *, dialect_name: str) -> None:
    """Replace the map's junction projection rows with rows derived from content.

    Mirrors ``derive_lifecycle_map_stages`` in ``modulo.core.lifecycle_map.service``:
    delete + re-insert, skipping non-dict stages and non-UUID pipeline ids
    (content_json remains the source of truth). The generated ``id`` is
    stringified on SQLite, whose driver cannot bind raw ``uuid.UUID`` objects.
    """
    conn.execute(_DELETE_JUNCTION_SQL, {"map_id": row.id})
    stages = content.get("stages") if isinstance(content, dict) else None
    if not isinstance(stages, list):
        return
    for position, stage in enumerate(stages):
        if not isinstance(stage, dict):
            continue
        pipeline_id: Any = None
        pipeline_raw = stage.get("pipeline_id")
        if isinstance(pipeline_raw, str) and pipeline_raw.strip():
            try:
                pipeline_id = uuid.UUID(pipeline_raw)
            except ValueError:
                pipeline_id = None
        if pipeline_id is not None and dialect_name == "sqlite":
            pipeline_id = str(pipeline_id)
        new_id: Any = uuid.uuid4() if dialect_name != "sqlite" else str(uuid.uuid4())
        conn.execute(
            _INSERT_JUNCTION_SQL,
            {
                "id": new_id,
                "map_id": row.id,
                "version": row.version,
                "stage_id": stage.get("id", ""),
                "stage_name": stage.get("name", ""),
                "position": position,
                "stage_type": stage.get("type", "placeholder"),
                "pipeline_id": pipeline_id,
                "organisation_id": row.organisation_id,
                "account_id": row.account_id,
            },
        )


def _scan_maps(conn: Any, *, after_id: Any, batch_size: int) -> list[Any]:
    """One keyset batch of maps (``id > after_id``; all maps on the first batch).

    The predicate is added only when ``after_id`` is set so the first batch does
    not bind ``id > NULL`` (which is always false and would scan nothing).
    """
    stmt = "SELECT id, organisation_id, account_id, version, content_json FROM lifecycle_maps"
    params: dict[str, Any] = {"batch_size": batch_size}
    if after_id is not None:
        stmt += " WHERE id > :after_id"
        params["after_id"] = after_id
    stmt += " ORDER BY id LIMIT :batch_size"
    return cast(list[Any], conn.execute(text(stmt), params).fetchall())


def _run_backfill(conn: Any, *, apply: bool, batch_size: int, limit: int | None) -> dict[str, Any]:
    """Walk maps in PK batches, repairing legacy content where detected.

    Returns a summary dict: ``mode``, ``batches``, ``maps_scanned``,
    ``maps_repaired``, ``skips`` (list of ``(map_id, reason)``) and ``sample``
    (map ids a dry-run would repair, up to 5).
    """
    after_id: Any = None
    batches = 0
    scanned_total = 0
    repaired = 0
    skips: list[tuple[str, str]] = []
    sample: list[tuple[str, list[str]]] = []
    dialect_name = getattr(getattr(conn, "dialect", None), "name", "")

    while True:
        if limit is not None and scanned_total >= limit:
            break
        batch_limit = batch_size if limit is None else min(batch_size, limit - scanned_total)
        if batch_limit <= 0:
            break
        rows = _scan_maps(conn, after_id=after_id, batch_size=batch_limit)
        if not rows:
            break
        batches += 1
        for row in rows:
            if limit is not None and scanned_total >= limit:
                break
            scanned_total += 1
            map_id = str(row.id)
            cleaned, changes, ok, reason = _clean_map(_load_content(row.content_json))
            if not ok:
                _log.warning(
                    "backfill_lifecycle_map_legacy_content.skip",
                    extra={"map_id": map_id, "reason": reason or "unknown"},
                )
                skips.append((map_id, reason or "unknown"))
                continue
            if not changes:
                continue
            assert cleaned is not None, "repairable content must be returned when ok"
            repaired += 1
            if apply:
                conn.execute(
                    _UPDATE_SQL,
                    {
                        "content": _bind_json_param(cleaned, dialect_name),
                        "map_id": row.id,
                    },
                )
                _reproject_junction(conn, row, cleaned, dialect_name=dialect_name)
            elif len(sample) < 5:
                sample.append((map_id, changes))
        # Commit each batch so a crash never rolls back already-repaired maps
        # (engine.connect() autobegins a transaction that would otherwise roll
        # back silently on close).
        if apply:
            conn.commit()
        after_id = rows[-1].id
        if len(rows) < batch_limit:
            break

    return {
        "mode": "apply" if apply else "dry-run",
        "batches": batches,
        "maps_scanned": scanned_total,
        "maps_repaired": repaired,
        "skips": skips,
        "sample": sample,
    }


def _backfill_with_engine(
    engine: Any,
    *,
    apply: bool,
    batch_size: int,
    limit: int | None,
) -> dict[str, Any]:
    """Run the backfill on a single connection, rolling back on failure.

    ``engine.connect()`` autobegins a transaction; without an explicit
    ``rollback()`` here the failed connection would be closed mid-transaction.
    Commits are per-batch inside ``_run_backfill``, so a mid-run failure keeps
    every completed batch while discarding the in-flight one.
    """
    conn: Any = None
    try:
        with engine.connect() as connection:
            conn = connection
            return _run_backfill(
                connection,
                apply=apply,
                batch_size=batch_size,
                limit=limit,
            )
    except Exception:
        if conn is not None:
            conn.rollback()
        raise


def _print_summary(summary: dict[str, Any], out: TextIO) -> None:
    print(f"Backfill summary ({summary['mode']}):", file=out)
    print(f"  batches: {summary['batches']}", file=out)
    print(f"  maps scanned: {summary['maps_scanned']}", file=out)
    print(f"  maps repaired: {summary['maps_repaired']}", file=out)
    print(f"  maps left for manual repair: {len(summary['skips'])}", file=out)
    for map_id, reason in summary["skips"]:
        print(f"    - {map_id}: {reason}", file=out)
    if summary["mode"] == "dry-run" and summary["sample"]:
        print("  sample of would-be repaired maps:", file=out)
        for map_id, changes in summary["sample"]:
            print(f"    - {map_id}: {changes[0] if changes else 'clean'}", file=out)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="backfill_lifecycle_map_legacy_content.py",
        description="Repair legacy lifecycle-map content_json blocked by FAR-175 validation.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the repaired content_json + junction projection; without this the "
        "run is a dry-run that writes nothing.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Maps per primary-key batch (default: 100).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Bound the run to at most this many maps (bounded test run).",
    )
    args = parser.parse_args()

    raw = os.environ.get("DATABASE_URL")
    if not raw:
        print("ERROR: DATABASE_URL is not set.", file=sys.stderr)
        sys.exit(1)
    engine = create_engine(_resolve_db_url(raw))
    try:
        summary = _backfill_with_engine(
            engine,
            apply=args.apply,
            batch_size=args.batch_size,
            limit=args.limit,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        engine.dispose()
    _print_summary(summary, sys.stdout)
    # Gate: a map the cleaner cannot repair must not report success.
    sys.exit(1 if summary["skips"] else 0)


if __name__ == "__main__":
    main()
