"""Final-schema coverage replacing the deleted 0330 migration tests."""

from pathlib import Path

_BASELINE = Path(__file__).parents[3] / "src" / "farna" / "db" / "migrations" / "versions" / "0001_baseline_schema.py"


def test_eval_results_final_schema_is_in_the_baseline() -> None:
    source = _BASELINE.read_text(encoding="utf-8")
    assert "CREATE TABLE public.eval_results" in source
    assert "outcome_json" in source
