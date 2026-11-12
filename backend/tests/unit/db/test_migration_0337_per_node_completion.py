"""Final-schema coverage replacing the deleted per-node completion migration."""

from pathlib import Path

_BASELINE = Path(__file__).parents[3] / "src" / "farna" / "db" / "migrations" / "versions" / "0350_baseline_schema.py"


def test_run_node_outputs_final_schema_is_in_the_baseline() -> None:
    source = _BASELINE.read_text(encoding="utf-8")
    table_start = source.index("CREATE TABLE public.run_node_outputs")
    table_end = source.index("CREATE TABLE", table_start + 1)
    table = source[table_start:table_end]
    assert "completed_at timestamp with time zone" in table
