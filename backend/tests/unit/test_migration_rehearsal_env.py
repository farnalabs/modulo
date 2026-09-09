"""Unit tests for env.py's rehearsal-mode helpers (FAR-717).

Importing :mod:`modulo.db.migrations.env` outside an alembic run is safe:
its module-level config block detects the unset context and skips the
online/offline execution entirely, so the rehearsal guard helpers are
testable without a database. Container-backed rehearsal behaviour (real
rollback, real upgrade, escape-hatch refusal through the live chain) is
covered by ``tests/integration/db/test_migration_rehearsal.py``.
"""

from unittest.mock import MagicMock

import pytest

from modulo.db.migrations import env as rehearsal_env


@pytest.fixture
def rehearsal_flags_unset():
    """Guarantee mode flags are False after every test that toggles them."""
    yield
    rehearsal_env._rehearsal_mode = False
    rehearsal_env._rehearsal_main_conn_acquired = False
    rehearsal_env._rehearsal_planned_steps = []
    rehearsal_env._rehearsal_applied_steps = []


def _arm(culprit_plan: list[str] | None = None) -> None:
    rehearsal_env._rehearsal_mode = True
    rehearsal_env._rehearsal_main_conn_acquired = True
    rehearsal_env._rehearsal_planned_steps = culprit_plan or []


def test_guard_refuses_with_named_revision(rehearsal_flags_unset) -> None:
    _arm(culprit_plan=["0193_index", "0195_helpers", "0194_soundness"])
    rehearsal_env._rehearsal_applied_steps = ["0193_index"]
    with pytest.raises(RuntimeError) as err:
        rehearsal_env._refuse_rehearsal_escape_hatch()
    message = str(err.value)
    assert "migration 0195_helpers uses the autocommit escape hatch" in message
    assert "CANNOT be rehearsed" in message
    assert "its effects would persist" in message
    assert "exclude it from rehearsal or run it for real" in message


def test_guard_refuses_preflight_widening_when_no_step_completed(rehearsal_flags_unset) -> None:
    _arm()
    with pytest.raises(RuntimeError) as err:
        rehearsal_env._refuse_rehearsal_escape_hatch("the pre-flight alembic_version widening")
    message = str(err.value)
    assert "the pre-flight alembic_version widening uses the autocommit escape hatch" in message
    assert "CANNOT be rehearsed" in message


def test_guard_is_noop_outside_rehearsal_mode() -> None:
    rehearsal_env._rehearsal_mode = False
    rehearsal_env._rehearsal_main_conn_acquired = True
    refusal = _guard_silent_call("any culprit")
    assert refusal is None


def test_guard_is_noop_before_main_connection_acquired(rehearsal_flags_unset) -> None:
    # The plan connection and the advisory-lock connection are legally
    # checked out before the rehearsal's main connection exists.
    rehearsal_env._rehearsal_mode = True
    rehearsal_env._rehearsal_main_conn_acquired = False
    refusal = _guard_silent_call("any culprit")
    assert refusal is None


def _guard_silent_call(culprit: str) -> str | None:
    """Invoke the guard, capturing its refusal (None when it stays silent)."""
    try:
        rehearsal_env._refuse_rehearsal_escape_hatch(culprit)
    except RuntimeError as exc:
        return str(exc)
    return None


def test_side_connection_listener_refuses(rehearsal_flags_unset) -> None:
    _arm(culprit_plan=["0195_helpers"])
    rehearsal_env._rehearsal_applied_steps = []
    with pytest.raises(RuntimeError) as err:
        rehearsal_env._refuse_rehearsal_side_connection(None, None, None)
    assert "migration 0195_helpers uses the autocommit escape hatch" in str(err.value)


def test_culprit_falls_back_to_final_step_outside_the_plan(rehearsal_flags_unset) -> None:
    _arm(culprit_plan=["0195_helpers"])
    rehearsal_env._rehearsal_applied_steps = ["0195_helpers"]
    assert rehearsal_env._rehearsal_culprit_revision() == "migration 0195_helpers (final planned step)"


def test_failure_report_names_next_planned_step(rehearsal_flags_unset) -> None:
    rehearsal_env._rehearsal_planned_steps = ["a1", "a2", "a3"]
    rehearsal_env._rehearsal_applied_steps = ["a1"]
    assert rehearsal_env._rehearsal_failure_step() == "a2"


def test_failure_report_before_any_step(rehearsal_flags_unset) -> None:
    rehearsal_env._rehearsal_planned_steps = []
    rehearsal_env._rehearsal_applied_steps = []
    assert rehearsal_env._rehearsal_failure_step() == "(before any migration step applied)"


def test_failure_report_finalisation(rehearsal_flags_unset) -> None:
    rehearsal_env._rehearsal_planned_steps = ["a1"]
    rehearsal_env._rehearsal_applied_steps = ["a1"]
    assert rehearsal_env._rehearsal_failure_step() == "a1 (finalisation)"


def test_step_callback_records_completed_revisions() -> None:
    class _Step:
        def __init__(self, up: str) -> None:
            self.up_revision_id = up

    rehearsal_env._rehearsal_applied_steps = []
    rehearsal_env._rehearsal_current_revision = None
    try:
        rehearsal_env._rehearsal_step_applied(ctx=None, step=_Step("0195_helpers"), heads=(), run_args={})
        rehearsal_env._rehearsal_step_applied(ctx=None, step=_Step("0194_soundness"), heads=(), run_args={})
        assert rehearsal_env._rehearsal_applied_steps == ["0195_helpers", "0194_soundness"]
        assert rehearsal_env._rehearsal_current_revision == "0194_soundness"
    finally:
        rehearsal_env._rehearsal_applied_steps = []
        rehearsal_env._rehearsal_current_revision = None


def test_step_callback_ignores_missing_revision() -> None:
    rehearsal_env._rehearsal_applied_steps = []
    rehearsal_env._rehearsal_current_revision = "0194_soundness"
    try:
        rehearsal_env._rehearsal_step_applied(ctx=None, step=None, heads=(), run_args={})
        assert not rehearsal_env._rehearsal_applied_steps
        assert rehearsal_env._rehearsal_current_revision == "0194_soundness"
    finally:
        rehearsal_env._rehearsal_applied_steps = []
        rehearsal_env._rehearsal_current_revision = None


def test_to_sync_url_maps_asyncpg_to_psycopg() -> None:
    # Regression for FAR-717 review finding #1: the sync URL the rehearsal
    # fixtures build must select the postgresql+psycopg dialect (psycopg v3 /
    # psycopg-binary), NOT postgresql+psycopg2 — psycopg2 is not in the
    # dependency tree, so a bare postgresql:// mapping blows up at fixture
    # setup with ModuleNotFoundError: No module named 'psycopg2'.
    url = rehearsal_env._to_sync_url("postgresql+asyncpg://u:p@host:5432/db")
    assert url.startswith("postgresql+psycopg://")
    assert "asyncpg" not in url
    assert "psycopg2" not in url


def test_rehearsal_plan_returns_upgrade_order(monkeypatch, rehearsal_flags_unset) -> None:
    # Regression for FAR-717 review finding #2: ScriptDirectory.iterate_revisions
    # yields NEWEST-first (head -> current), but the chain is APPLIED
    # oldest-first. _rehearsal_plan must reverse that into real application
    # order so the failing-step report names the step actually in flight.
    class _FakeScript:
        def __init__(self, rev: str) -> None:
            self.revision = rev

    # alembic's iterate_revisions yields head-first: newest (c) -> oldest (a).
    newest_first = [_FakeScript(r) for r in ["c", "b", "a"]]
    fake_script = MagicMock()
    fake_script.get_current_head.return_value = "c"
    fake_script.iterate_revisions.return_value = newest_first

    # engine.connect() is only used to read the DB's current revision; the
    # plan never needs a real connection for this assertion.
    fake_engine = MagicMock()
    # env.py's module-global `config` is None outside an alembic run; stub it so
    # _rehearsal_plan does not short-circuit with "Alembic env config unavailable".
    monkeypatch.setattr(rehearsal_env, "config", MagicMock())
    monkeypatch.setattr(rehearsal_env, "ScriptDirectory", lambda cfg: fake_script)
    monkeypatch.setattr(
        rehearsal_env,
        "MigrationContext",
        MagicMock(configure=MagicMock(return_value=MagicMock(get_current_revision=MagicMock(return_value="a")))),
    )

    planned, db_current, script_head = rehearsal_env._rehearsal_plan(fake_engine)

    # iterate_revisions was asked for the chain from head down to current.
    fake_script.iterate_revisions.assert_called_once_with("c", "a")
    assert script_head == "c"
    assert db_current == "a"
    # Reversed into application order: oldest first.
    assert [step.revision for step in planned] == ["a", "b", "c"]


def test_rehearsal_failure_step_pins_reversed_mapping(rehearsal_flags_unset) -> None:
    # The reversed plan (oldest-first) must align with _rehearsal_applied_steps
    # so the reported failing revision is the in-flight one, not a misaligned
    # entry. A first-step failure (nothing applied yet) names the OLDEST
    # revision, never the newest.
    rehearsal_env._rehearsal_planned_steps = ["a", "b", "c"]  # upgrade order
    rehearsal_env._rehearsal_applied_steps = []  # failure before any step ran
    assert rehearsal_env._rehearsal_failure_step() == "a"


def test_rehearsal_requested_reads_env_var(monkeypatch) -> None:
    monkeypatch.delenv("ALEMBIC_REHEARSAL", raising=False)
    assert rehearsal_env._rehearsal_requested() is False
    monkeypatch.setenv("ALEMBIC_REHEARSAL", "1")
    assert rehearsal_env._rehearsal_requested() is True
    monkeypatch.setenv("ALEMBIC_REHEARSAL", "0")
    assert rehearsal_env._rehearsal_requested() is False
