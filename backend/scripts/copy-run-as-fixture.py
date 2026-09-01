"""
Export a run's input/output data as a pytest fixture for regression testing.

Usage:
    uv run python scripts/copy-run-as-fixture.py <run-uuid>
    uv run python scripts/copy-run-as-fixture.py <run-uuid> --output tests/fixtures/runs
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy import select

from modulo.api.dependencies import get_or_create_engine, get_or_create_session_factory
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.settings import Settings


def _safe_output_dir(path: str) -> Path:
    """Resolve *path* and require it to stay within the working directory."""
    resolved = os.path.realpath(path)
    base = os.path.realpath(Path.cwd())
    if resolved != base and not resolved.startswith(base + os.sep):
        raise ValueError(f"output directory {path!r} resolves outside the working directory")
    return Path(resolved)


def build_fixture_map(
    input_payload: dict | None,
    outputs_json: dict | None,
) -> dict[str, str]:
    """Generate a StubModelBackend fixture_map from run IO.

    If outputs_json is structured per-node (each value is a dict with
    ``input`` and ``output`` keys), each node's mapping becomes a
    fixture_map entry.  Otherwise a single entry maps the full
    input_payload to the serialised outputs.
    """
    fixture: dict[str, str] = {}
    inp = input_payload or {}
    out = outputs_json or {}

    if isinstance(out, dict) and any(isinstance(v, dict) and "input" in v and "output" in v for v in out.values()):
        for node_io in out.values():
            if isinstance(node_io, dict):
                node_input = node_io.get("input", str(inp))
                node_output = node_io.get("output", "")
                key = " ".join(str(node_input).split())
                fixture[key] = str(node_output)
    else:
        key = " ".join(str(inp).split())
        fixture[key] = str(out)

    return fixture


async def fetch_run_fixture_data(run_id: str) -> dict:
    """Fetch run + snapshot from DB and return serialisable fixture data."""
    settings = Settings()
    engine = get_or_create_engine(settings)
    factory = get_or_create_session_factory(engine)

    run_uuid = uuid.UUID(run_id)

    async with factory() as session:
        result = await session.execute(select(Run).where(Run.id == run_uuid))
        run = result.scalar_one_or_none()

    if run is None:
        print(f"Run {run_id} not found", file=sys.stderr)
        sys.exit(1)

    async with factory() as session:
        snap_result = await session.execute(select(PipelineSnapshot).where(PipelineSnapshot.id == run.snapshot_id))
        snapshot = snap_result.scalar_one_or_none()

    graph_json = snapshot.graph_json if snapshot else {}
    fixture_map = build_fixture_map(run.input_payload, run.outputs_json)

    short_id = str(run.id).split("-")[0]
    return {
        "fixture_name": f"run_{short_id}_io",
        "run_id": str(run.id),
        "pipeline_id": str(run.pipeline_id),
        "status": run.status,
        "snapshot_graph_json": graph_json,
        "input_payload": run.input_payload,
        "outputs_json": run.outputs_json,
        "fixture_map": fixture_map,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy run as test fixture")
    parser.add_argument("run_id", help="Run UUID to export")
    parser.add_argument(
        "--output",
        "-o",
        default="tests/fixtures/runs",
        help="Output directory (default: tests/fixtures/runs)",
    )
    args = parser.parse_args()

    fixture = asyncio.run(fetch_run_fixture_data(args.run_id))

    output_dir = _safe_output_dir(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{fixture['run_id']}.json"
    data = json.dumps(fixture, indent=2, default=str, ensure_ascii=False) + "\n"
    output_path.write_text(data, encoding="utf-8")
    print(f"Fixture written to {output_path.resolve()}", file=sys.stderr)


if __name__ == "__main__":
    main()
