"""FAR-2285 durable-evidence clause (``observed_activity_expr``) unit tests.

The reclaim / shutdown-handback gate asks the blob store whether a run
produced DURABLE, engine-observed activity; the answer decides whether a
re-run is safe. The clause lives in ``db/crud/run_node_outputs`` because that
module owns every read of those columns (FAR-583), so it is tested here.

These pin the compiled SQL shape. The end-to-end behaviour (self-reported-only
requeues, engine evidence terminalises) is pinned against a real database by
``tests/integration/test_far1627_shutdown_handoff.py``.
"""

from __future__ import annotations

from sqlalchemy import literal_column, select
from sqlalchemy.dialects import postgresql

from modulo.db.crud.run_node_outputs import observed_activity_expr


def _compiled() -> tuple[str, list[object]]:
    statement = select(
        observed_activity_expr(
            run_id_column=literal_column("run_id"),
            organisation_id_column=literal_column("organisation_id"),
        )
    )
    compiled = statement.compile(dialect=postgresql.dialect())
    return str(compiled), list(compiled.params.values())


def test_clause_is_correlated_to_the_run_and_its_organisation() -> None:
    """The EXISTS must not see another run's — or another org's — evidence."""
    sql, _params = _compiled()

    assert "run_node_outputs.run_id = run_id" in sql
    assert "run_node_outputs.organisation_id = organisation_id" in sql


def test_clause_counts_engine_evidence_and_durable_artifacts() -> None:
    """Observed lengths, retained tails, markers, artifacts and schema records."""
    sql, params = _compiled()

    assert "stdout_length" in params
    assert "sandbox_log_tail" in params
    assert "raw_output_markers" in sql
    assert "artifacts_json" in sql
    assert "schema_enforcement_json" in sql


def test_clause_ignores_self_reported_agent_returns() -> None:
    """A self-reported-only envelope must never classify as engine evidence."""
    _sql, params = _compiled()

    assert "output_json" not in params
    assert "provenance" not in params


def test_clause_tolerates_nested_legacy_shapes() -> None:
    """One flat scan for P1+ rows, one nested scan for pre-split rows."""
    sql, _params = _compiled()

    assert sql.count("jsonb_each") >= 2
