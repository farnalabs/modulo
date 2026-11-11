"""Final-schema coverage replacing the deleted runs blob-drop migration."""

from pathlib import Path

_BASELINE = Path(__file__).parents[3] / "src" / "farna" / "db" / "migrations" / "versions" / "0001_baseline_schema.py"


def test_final_run_node_outputs_columns_are_present() -> None:
    source = _BASELINE.read_text(encoding="utf-8")
    assert "CREATE TABLE public.run_node_outputs" in source
    assert "outputs_json jsonb" in source
    assert "node_telemetry_json jsonb" in source
    assert "raw_output_markers jsonb" in source
