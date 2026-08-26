"""Model- and behaviour-level tests for SuiteRun (FAR-376 Phase 3).

Covers, without Docker (in-memory SQLite + pure functions):

* the state machine — legal + illegal transitions, and the optimistic-lock
  version guard that keeps two concurrent writers from both landing a terminal
  state;
* deterministic canonical signature helpers (dataset version + definition
  checksum + scenario signature);
* the immutable baseline tuple and its deterministic resolution, incl. the
  cross-org isolation rule (a cross-org run is NEVER selected as a baseline);
* ORM-level org isolation via the generic-backend tenant filter (the SQLite
  counterpart to the Postgres ``rls_org_isolation`` policy);
* spend — two INDEPENDENT counters (daily + per-suite cumulative) and the
  atomic increment-before-judge claim;
* notification isolation — no forwarder leakage and the silent-drop guard.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from modulo.core.eval_engine.suite_run import (
    SuiteRunError,
    assert_eval_notification_isolated,
    build_baseline_tuple,
    daily_spend_exceeded,
    is_suite_rate_limited,
    load_eval_subscriber_events,
    pass_rate_by_eval_type,
    resolve_baseline_run,
    should_notify_regression,
    suite_alert_metrics,
    suite_cumulative_exceeded,
)
from modulo.db.models import (
    Base,
    EvalResult,
    SuiteRun,
    compute_definition_checksum,
    compute_scenario_signature,
)
from modulo.db.models.eval_suite_run import (
    IllegalStateTransitionError,
    OptimisticLockError,
    SuiteRunState,
    can_transition,
    is_terminal,
    transition_state,
)
from modulo.db.rls import _inject_tenant_filter

_TENANT_KEY = "org_id"
_MIGRATION_PATH = (
    Path(__file__).parents[4]
    / "backend"
    / "src"
    / "modulo"
    / "db"
    / "migrations"
    / "versions"
    / "0137_eval_suite_run.py"
)


def _make_session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[SuiteRun.__table__, EvalResult.__table__])
    return Session(engine)


def _new_run(org_id: uuid.UUID, state: str = "pending", **kw: Any) -> SuiteRun:
    return SuiteRun(
        organisation_id=org_id,
        suite_id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        dataset_version=1,
        definition_checksum="a" * 64,
        model_backend_id=uuid.uuid4(),
        state=state,
        **kw,
    )


# --------------------------------------------------------------------------- #
# Schema                                                                      #
# --------------------------------------------------------------------------- #
def test_suite_runs_table_exists() -> None:
    assert "suite_runs" in Base.metadata.tables


def test_suite_runs_columns() -> None:
    cols = Base.metadata.tables["suite_runs"].c
    for name in (
        "id",
        "organisation_id",
        "owner_team_id",
        "suite_id",
        "dataset_id",
        "dataset_version",
        "definition_checksum",
        "model_backend_id",
        "scenario_signature",
        "baseline_tuple",
        "baseline_run_id",
        "baseline_locked",
        "state",
        "version",
        "total_cases",
        "passed_cases",
        "failed_cases",
        "excluded_case_count",
        "total_cost_usd",
        "claimed_cost",
        "comparison_json",
        "regressed",
        "notified_at",
        "completed_at",
        "error_detail",
        "extra",
        "created_at",
        "updated_at",
    ):
        assert name in cols


def test_suite_runs_is_org_scoped() -> None:
    from modulo.db.models.base import OrgScoped

    assert issubclass(SuiteRun, OrgScoped)


def test_eval_results_has_suite_run_id() -> None:
    cols = Base.metadata.tables["eval_results"].c
    assert "suite_run_id" in cols
    fk = [f for f in EvalResult.__table__.foreign_keys if f.column.table.name == "suite_runs"]
    assert fk, "eval_results.suite_run_id FK to suite_runs missing"


# --------------------------------------------------------------------------- #
# Signature helpers                                                           #
# --------------------------------------------------------------------------- #
def test_definition_checksum_deterministic_and_order_insensitive() -> None:
    definition_id = uuid.uuid4()
    left = compute_definition_checksum([{"id": definition_id, "config_json": {"pattern": "a|b"}}])
    right = compute_definition_checksum([{"id": definition_id, "config_json": {"pattern": "a|b"}}])
    assert left == right
    assert len(left) == 64
    changed = compute_definition_checksum([{"id": definition_id, "config_json": {"pattern": "a|c"}}])
    assert changed != left
    # membership order does not matter (sorted by id).
    other_id = uuid.uuid4()
    a = compute_definition_checksum(
        [{"id": definition_id, "config_json": {"pattern": "a|b"}}, {"id": other_id, "config_json": {"pattern": "z"}}]
    )
    b = compute_definition_checksum(
        [{"id": other_id, "config_json": {"pattern": "z"}}, {"id": definition_id, "config_json": {"pattern": "a|b"}}]
    )
    assert a == b


def test_scenario_signature_null_when_scenarios_unused() -> None:
    assert compute_scenario_signature(None) is None
    assert compute_scenario_signature({}) is None
    sig = compute_scenario_signature({"user": {"lang": "en"}, "k": [1, 2]})
    assert sig is not None
    assert len(sig) == 64
    # canonical (key-order free)
    assert compute_scenario_signature({"b": 1, "a": 2}) == compute_scenario_signature({"a": 2, "b": 1})


def test_build_baseline_tuple_is_snapshot() -> None:
    tup = build_baseline_tuple(
        suite_id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        dataset_version=1,
        eval_definition_ids=[uuid.uuid4(), uuid.uuid4()],
        definition_checksum="c" * 64,
        model_backend_id=uuid.uuid4(),
        scenario_signature=None,
    )
    assert tup["dataset_version"] == 1
    assert tup["scenario_signature"] is None
    assert tup["eval_definition_ids"] == sorted(tup["eval_definition_ids"])
    assert len(tup["definition_checksum"]) == 64


# --------------------------------------------------------------------------- #
# State machine                                                               #
# --------------------------------------------------------------------------- #
def test_legal_transition_chain() -> None:
    session = _make_session()
    run = _new_run(uuid.uuid4())
    session.add(run)
    session.flush()
    v1 = transition_state(session, run, SuiteRunState.RUNNING)
    assert v1 == 1
    assert run.state == "running"
    assert run.version == 1
    v2 = transition_state(session, run, SuiteRunState.COMPLETED, completed_at=datetime.now(UTC))
    assert v2 == 2
    assert run.state == "completed"
    assert run.completed_at is not None
    session.close()


def test_illegal_transition_raises() -> None:
    session = _make_session()
    run = _new_run(uuid.uuid4())
    session.add(run)
    session.flush()
    transition_state(session, run, SuiteRunState.RUNNING)
    transition_state(session, run, SuiteRunState.COMPLETED)
    # completed -> running is not legal
    with pytest.raises(IllegalStateTransitionError):
        transition_state(session, run, SuiteRunState.RUNNING)
    session.close()


def test_optimistic_lock_rejects_second_writer() -> None:
    session = _make_session()
    run = _new_run(uuid.uuid4())
    session.add(run)
    session.flush()
    first = transition_state(session, run, SuiteRunState.RUNNING)
    assert first == 1
    # A second writer that read the same pre-transition version loses the race.
    with pytest.raises(OptimisticLockError, match="already bumped"):
        transition_state(session, run, SuiteRunState.COMPLETED, expected_version=0)
    session.close()


def test_terminal_states_have_no_outgoing_edges() -> None:
    for terminal in (SuiteRunState.COMPLETED, SuiteRunState.PARTIAL, SuiteRunState.FAILED, SuiteRunState.CANCELLED):
        assert is_terminal(terminal)
        for nxt in SuiteRunState:
            assert not can_transition(terminal, nxt)


def test_partial_distinct_from_failed() -> None:
    session = _make_session()
    run = _new_run(uuid.uuid4())
    session.add(run)
    session.flush()
    transition_state(session, run, SuiteRunState.RUNNING)
    transition_state(session, run, SuiteRunState.PARTIAL)
    assert run.state == "partial"
    assert is_terminal(run.state)
    session.close()


# --------------------------------------------------------------------------- #
# Baseline resolution (pure selection against a mock async session)           #
# --------------------------------------------------------------------------- #
class _FakeScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


async def _resolve(run: SuiteRun, candidates: list[SuiteRun]) -> tuple[SuiteRun | None, str | None]:
    session = AsyncMock()
    session.scalars.return_value = _FakeScalarResult(candidates)
    return await resolve_baseline_run(session, run)


def _completed(org: uuid.UUID, *, created_at: datetime, tuple_sig: str, run_id: uuid.UUID | None = None) -> SuiteRun:
    return SuiteRun(
        id=run_id or uuid.uuid4(),
        organisation_id=org,
        suite_id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        dataset_version=1,
        definition_checksum="a" * 64,
        model_backend_id=uuid.uuid4(),
        baseline_tuple={"definition_checksum": tuple_sig},
        state="completed",
        created_at=created_at,
    )


async def test_baseline_first_run_warns_and_skips() -> None:
    current = _completed(uuid.uuid4(), created_at=datetime.now(UTC), tuple_sig="sig")
    baselines, warning = await _resolve(current, [])
    assert baselines == []
    assert warning is not None
    assert "comparison skipped" in warning


async def test_baseline_picks_latest_completed_same_tuple_prior() -> None:
    org = uuid.uuid4()
    now = datetime.now(UTC)
    earlier = _completed(org, created_at=now - timedelta(hours=2), tuple_sig="sig")
    latest = _completed(org, created_at=now - timedelta(hours=1), tuple_sig="sig")
    current = _completed(org, created_at=now, tuple_sig="sig")
    baselines, warning = await _resolve(current, [earlier, latest])
    assert baselines
    assert baselines[0].id == latest.id
    assert warning is None


async def test_baseline_ignores_non_completed_and_other_tuple() -> None:
    org = uuid.uuid4()
    now = datetime.now(UTC)
    pending = _completed(org, created_at=now - timedelta(hours=3), tuple_sig="sig")
    pending.state = "pending"
    diff_tuple = _completed(org, created_at=now - timedelta(hours=2), tuple_sig="OTHER")
    current = _completed(org, created_at=now, tuple_sig="sig")
    baselines, _ = await _resolve(current, [pending, diff_tuple])
    assert baselines == []


async def test_baseline_cross_org_never_selected() -> None:
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    now = datetime.now(UTC)
    cross_org = _completed(org_b, created_at=now - timedelta(hours=1), tuple_sig="sig")
    current = _completed(org_a, created_at=now, tuple_sig="sig")
    baselines, warning = await _resolve(current, [cross_org])
    assert baselines == []
    assert warning is not None


async def test_baseline_tiebreak_is_lexical_id() -> None:
    org = uuid.uuid4()
    now = datetime.now(UTC)
    low_id = _completed(org, created_at=now - timedelta(hours=1), tuple_sig="sig", run_id=uuid.uuid4())
    high_id = _completed(org, created_at=now - timedelta(hours=1), tuple_sig="sig", run_id=uuid.uuid4())
    low, high = sorted([low_id, high_id], key=lambda r: str(r.id))
    current = _completed(org, created_at=now, tuple_sig="sig")
    baselines, _ = await _resolve(current, [low, high])
    assert baselines
    assert baselines[0].id == high.id


async def test_baseline_window_returns_n_most_recent() -> None:
    """FAR-379: a suite ``baseline_window`` of N widens the baseline to the N
    most-recent completed same-tuple prior runs instead of only the latest.
    Without the fix (window ignored) only one baseline is returned."""
    org = uuid.uuid4()
    now = datetime.now(UTC)
    runs = [_completed(org, created_at=now - timedelta(hours=h), tuple_sig="sig") for h in (5, 4, 3, 2, 1)]
    current = _completed(org, created_at=now, tuple_sig="sig")
    session = AsyncMock()
    session.scalars.return_value = _FakeScalarResult(runs)
    baselines, warning = await resolve_baseline_run(session, current, baseline_window=3)
    assert warning is None
    assert len(baselines) == 3
    # Most-recent-first: the last (h=1) run is the single latest.
    assert baselines[0].id == runs[-1].id
    assert baselines[2].id == runs[-3].id
    # NULL window keeps the single-latest behaviour.
    single, warning_single = await resolve_baseline_run(session, current, baseline_window=None)
    assert warning_single is None
    assert len(single) == 1
    assert single[0].id == runs[-1].id


# --------------------------------------------------------------------------- #
# ORM org isolation (generic-backend tenant filter)                           #
# --------------------------------------------------------------------------- #
def _register_tenant_filter_on_session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[SuiteRun.__table__, EvalResult.__table__])
    event.listen(Session, "do_orm_execute", _inject_tenant_filter)
    return Session(engine)


def test_org_isolation_positive_control() -> None:
    session = _register_tenant_filter_on_session()
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    session.add_all([_new_run(org_a), _new_run(org_b)])
    session.commit()

    session.info[_TENANT_KEY] = org_a
    assert len(session.scalars(select(SuiteRun)).all()) == 1
    session.info[_TENANT_KEY] = org_b
    assert len(session.scalars(select(SuiteRun)).all()) == 1

    # Cross-org: an org that owns none of the rows sees zero.
    session.info[_TENANT_KEY] = uuid.uuid4()
    assert not session.scalars(select(SuiteRun)).all()
    session.close()


def test_org_isolation_no_tenant_key_sees_all() -> None:
    session = _register_tenant_filter_on_session()
    session.add_all([_new_run(uuid.uuid4()), _new_run(uuid.uuid4())])
    session.commit()
    session.info.pop(_TENANT_KEY, None)
    assert len(session.scalars(select(SuiteRun)).all()) == 2
    session.close()


# --------------------------------------------------------------------------- #
# Spend — two independent counters                                             #
# --------------------------------------------------------------------------- #
def test_daily_spend_independent_of_suite_ceiling() -> None:
    # Daily limit hit, suite ceiling not hit: only the daily counter refuses.
    assert daily_spend_exceeded(Decimal("10.0"), Decimal("10.0")) is True
    assert daily_spend_exceeded(Decimal("9.99"), Decimal("10.0")) is False
    assert daily_spend_exceeded(Decimal(5), None) is False
    assert suite_cumulative_exceeded(Decimal("20.0"), Decimal("20.0")) is True
    assert suite_cumulative_exceeded(Decimal("19.99"), Decimal("20.0")) is False
    assert suite_cumulative_exceeded(None, Decimal("20.0")) is False


# --------------------------------------------------------------------------- #
# Pass-rate aggregation (per eval_type ONLY)                                  #
# --------------------------------------------------------------------------- #
def test_pass_rate_by_eval_type_never_cross_combines() -> None:
    rows = [
        ("llm_judge", True),
        ("llm_judge", False),
        ("regex", True),
        ("regex", True),
        ("regex", True),
        ("human_set", False),
    ]
    by_type = pass_rate_by_eval_type(rows)
    assert by_type["llm_judge"]["pass_rate"] == 0.5
    assert by_type["regex"]["pass_rate"] == 1.0
    assert by_type["human_set"]["pass_rate"] == 0.0
    # each type is partitioned — llm_judge is not diluted by regex passes.
    assert by_type["llm_judge"]["total"] == 2
    assert by_type["regex"]["total"] == 3
    # mixed types must NOT be averaged together into a single score.
    assert set(by_type) == {"llm_judge", "regex", "human_set"}


def test_pass_rate_excludes_errored() -> None:
    from modulo.core.eval_engine.suite_run import suite_pass_rate

    results = [
        EvalResult(run_id=None, eval_id=uuid.uuid4(), passed=True),
        EvalResult(run_id=None, eval_id=uuid.uuid4(), passed=False),
    ]
    agg = suite_pass_rate(results, excluded_case_count=2)
    assert agg["pass_rate"] == 0.5
    assert agg["excluded"] == 2


# --------------------------------------------------------------------------- #
# Notification isolation                                                      #
# --------------------------------------------------------------------------- #
def test_eval_notification_must_have_eval_subscriber() -> None:
    with pytest.raises(SuiteRunError, match="no eval-scoped subscribers"):
        assert_eval_notification_isolated([])
    # eval subscribers with no forwarder overlap are fine.
    assert_eval_notification_isolated(["eval_regression"])


def test_eval_notification_rejects_error_forwarder_leak() -> None:
    with pytest.raises(SuiteRunError, match="leaks to error-forwarder"):
        assert_eval_notification_isolated(["eval_regression", "run_failed"])


def test_rate_limit_and_idempotency() -> None:
    run = SuiteRun(organisation_id=uuid.uuid4(), suite_id=uuid.uuid4(), dataset_id=uuid.uuid4())
    baseline = SuiteRun(organisation_id=uuid.uuid4(), suite_id=uuid.uuid4(), dataset_id=uuid.uuid4())
    assert should_notify_regression(run, baseline) is True
    assert should_notify_regression(run, None) is False  # no baseline → never alert
    run.notified_at = datetime.now(UTC)
    assert should_notify_regression(run, baseline) is False  # idempotent on suite_run_id


def test_suite_alert_metrics_derives_worst_alert() -> None:
    """The alert metrics summarise the worst (largest drop) alert already
    recorded in ``comparison_json`` — never re-doing the detection."""
    metrics = suite_alert_metrics(
        {
            "alerts": [
                {"prev_pass_rate": 0.9, "current_pass_rate": 0.6, "drop_pct": 0.3},
                {"prev_pass_rate": 0.8, "current_pass_rate": 0.3, "drop_pct": 0.5},
            ]
        }
    )
    assert metrics == {
        "alert_count": 2,
        "prev_pass_rate": 0.8,
        "current_pass_rate": 0.3,
        "drop_pct": 0.5,
    }


def test_suite_alert_metrics_none_when_no_alerts() -> None:
    assert suite_alert_metrics(None) is None
    assert suite_alert_metrics({}) is None
    assert suite_alert_metrics({"alerts": [], "regressed": True}) is None


def test_suite_rate_limit_window() -> None:
    assert is_suite_rate_limited(None, timedelta(hours=1)) is False
    assert is_suite_rate_limited(datetime.now(UTC), timedelta(hours=1)) is True
    assert is_suite_rate_limited(datetime.now(UTC) - timedelta(hours=2), timedelta(hours=1)) is False
    assert is_suite_rate_limited(datetime.now(UTC), None) is False


# --------------------------------------------------------------------------- #
# Migration: reversible + FORCE RLS + single head                             #
# --------------------------------------------------------------------------- #
def test_migration_applies_force_rls_on_suite_runs() -> None:
    text_content = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert "FORCE ROW LEVEL SECURITY" in text_content
    assert "CREATE POLICY rls_org_isolation" in text_content
    assert "ALTER TABLE suite_runs ENABLE ROW LEVEL SECURITY" in text_content
    assert '_TABLE = "suite_runs"' in text_content


def test_migration_is_reversible_single_head() -> None:
    text_content = _MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'op.drop_table("suite_runs")' in text_content
    assert 'drop_column("eval_results", "suite_run_id")' in text_content
    assert 'down_revision: str | None = "0136_rename_remy_user_id_to_account_id"' in text_content


def test_single_migration_head() -> None:
    """Exactly one migration chains off each predecessor, and the head is 0138."""
    import glob
    import re

    revisions = {}
    parents = {}
    for path in glob.glob(str(_MIGRATION_PATH.parent / "*.py")):
        with open(path, encoding="utf-8") as source_file:
            source = source_file.read()

        # Handle both `revision = "x"` and `revision: str = "x"` styles.
        def _value(line: str) -> str | None:
            match = re.search(r'"[^"]+"', line)
            return match.group(0).strip('"') if match else None

        rev = parent = None
        for line in source.splitlines():
            if "revision" in line and not line.strip().startswith("down_revision") and not line.strip().startswith('"'):
                candidate = _value(line)
                if candidate and candidate != _MIGRATION_PATH.name:
                    rev = candidate
            if line.strip().startswith("down_revision"):
                parent = _value(line)
        revisions[path] = rev
        parents[path] = parent

    def _basename(path: str) -> str:
        return path.replace("\\", "/").rsplit("/", 1)[-1]

    # The linear chain, integrated with main's run_evidence + dismissals RL
    # migrations + main's status_check_constraints + rename_remy migrations:
    # 0131 -> 0132 -> 0133_run_evidence_rls -> 0134_dismissals_org_rls
    # -> 0135_status_check_constraints -> 0136_rename_remy_user_id_to_account_id
    # -> 0137_eval_suite_run
    # -> 0138_eval_versioning (FAR-382).
    # -> 0140_eval_regression_alert (FAR-379 alerting config on eval_suites).
    # -> 0142_merge_heads_add_fk_indexes (main FK-index migration).
    # -> 0143_rest_connector_profile (FAR-412 REST connector profile, renumbered
    #    from 0142 to avoid colliding with main's 0142_merge_heads_add_fk_indexes).
    # -> 0144_broaden_notification_status_in_app (main: allow 'in_app' status on notification_delivery_log).
    # -> 0145_spend_ceiling (FAR-391, renumbered from 0144 to chain off the
    #    broaden_notification_status_in_app head and avoid colliding with main's 0144).
    # -> 0146_extend_runs_status_cost_ceiling (FAR-391, renumbered from 0145).
    # -> 0147_json_to_jsonb_standardize (db-jsonb-standardize renumber from 0145
    #    to avoid colliding with main's 0145_spend_ceiling, chains off 0146).
    # -> 0148_pipeline_snapshot_versioning_far420 (FAR-402 P6) chains off 0147_json_to_jsonb_standardize.
    chaining_off_0131 = [p for p in revisions if parents[p] == "0131_eval_dataset_corpus"]
    assert [_basename(p) for p in chaining_off_0131] == ["0132_agent_connector_report_soft_delete_audit.py"]
    chaining_off_0132 = [p for p in revisions if parents[p] == "0132_agent_connector_report_soft_delete_audit"]
    assert [_basename(p) for p in chaining_off_0132] == ["0133_run_evidence_rls.py"]
    chaining_off_0133 = [p for p in revisions if parents[p] == "0133_run_evidence_rls"]
    assert [_basename(p) for p in chaining_off_0133] == ["0134_dismissals_org_rls.py"]
    chaining_off_0134 = [p for p in revisions if parents[p] == "0134_dismissals_org_rls"]
    assert [_basename(p) for p in chaining_off_0134] == ["0135_status_check_constraints.py"]
    chaining_off_0135 = [p for p in revisions if parents[p] == "0135_status_check_constraints"]
    assert [_basename(p) for p in chaining_off_0135] == ["0136_rename_remy_user_id_to_account_id.py"]
    chaining_off_0136 = [p for p in revisions if parents[p] == "0136_rename_remy_user_id_to_account_id"]
    assert [_basename(p) for p in chaining_off_0136] == ["0137_eval_suite_run.py"]
    chaining_off_0137 = [p for p in revisions if parents[p] == "0137_eval_suite_run"]
    assert [_basename(p) for p in chaining_off_0137] == ["0138_eval_versioning.py"]
    # Nothing chains off 0138 except 0140 (the FAR-379 alerting migration).
    chaining_off_0138 = [p for p in revisions if parents[p] == "0138_eval_versioning"]
    assert [_basename(p) for p in chaining_off_0138] == ["0140_eval_regression_alert.py"]
    # 0141_pipeline_edge_ports (FAR-416, main) chains off 0140,
    # 0144_broaden_notification_status_in_app (main) chains off 0143, and
    # 0148_pipeline_snapshot_versioning_far420 (FAR-402 P6) chains off 0147_json_to_jsonb_standardize.
    chaining_off_0140 = [p for p in revisions if parents[p] == "0140_eval_regression_alert"]
    assert [_basename(p) for p in chaining_off_0140] == ["0141_pipeline_edge_ports.py"]
    # 0142_merge_heads_add_fk_indexes (main, FK-index pass) chains off 0141.
    chaining_off_0141 = [p for p in revisions if parents[p] == "0141_pipeline_edge_ports"]
    assert [_basename(p) for p in chaining_off_0141] == ["0142_merge_heads_add_fk_indexes.py"]
    # 0143_rest_connector_profile (FAR-412, main) chains off 0142.
    chaining_off_0142 = [p for p in revisions if parents[p] == "0142_merge_heads_add_fk_indexes"]
    assert [_basename(p) for p in chaining_off_0142] == ["0143_rest_connector_profile.py"]
    # Nothing chains off 0143 except 0144 (main's broaden notification status, in_app).
    # 0145_spend_ceiling (FAR-391) chains off 0144, 0146_extend_runs_status_cost_ceiling
    # chains off 0145, and this branch's 0147_json_to_jsonb_standardize chains off
    # 0146; 0148_pipeline_snapshot_versioning_far420 (FAR-402 P6) chains off 0147.
    chaining_off_0143 = [p for p in revisions if parents[p] == "0143_rest_connector_profile"]
    assert [_basename(p) for p in chaining_off_0143] == ["0144_broaden_notification_status_in_app.py"]
    # Nothing chains off 0144 except 0145 (FAR-391 spend ceiling, renumbered to chain off 0144).
    chaining_off_0144 = [p for p in revisions if parents[p] == "0144_broaden_notification_status_in_app"]
    assert [_basename(p) for p in chaining_off_0144] == ["0145_spend_ceiling.py"]
    # Nothing chains off 0145 except 0146 (FAR-391 extend runs status cost ceiling).
    chaining_off_0145 = [p for p in revisions if parents[p] == "0145_spend_ceiling"]
    assert [_basename(p) for p in chaining_off_0145] == ["0146_extend_runs_status_cost_ceiling.py"]
    # Nothing chains off 0146 except 0147 (this branch's json->jsonb cleanup).
    chaining_off_0146 = [p for p in revisions if parents[p] == "0146_extend_runs_status_cost_ceiling"]
    assert [_basename(p) for p in chaining_off_0146] == ["0147_json_to_jsonb_standardize.py"]
    # 0148_pipeline_snapshot_versioning_far420 (FAR-402 P6) chains off 0147 and is the head.
    chaining_off_0147 = [p for p in revisions if parents[p] == "0147_json_to_jsonb_standardize"]
    assert [_basename(p) for p in chaining_off_0147] == ["0148_pipeline_snapshot_versioning_far420.py"]
    # Nothing chains off 0148 -> it is the single head.
    chaining_off_0148 = [p for p in revisions if parents[p] == "0148_pipeline_snapshot_versioning_far420"]
    assert chaining_off_0148 == []


async def test_load_eval_subscriber_events_normalises_json() -> None:
    """The subscriber-event loader is exercised by the runtime guard."""
    session = AsyncMock()

    class Endpoint:
        def __init__(self, events: Any) -> None:
            self.events = events
            self.auto_disabled = False

    ep1 = Endpoint(["eval_regression"])
    ep2 = Endpoint('["eval_blocked"]')

    class FakeScalars:
        def __init__(self, rows: list[Any]) -> None:
            self._rows = rows

        def all(self) -> list[Any]:
            return self._rows

    session.scalars.return_value = FakeScalars([ep1, ep2])
    merged = await load_eval_subscriber_events(session, uuid.uuid4())
    assert merged == ["eval_regression", "eval_blocked"]
