"""Registration proof for the FAR-391 ``cost_ceiling_exceeded`` run status.

The spend-ceiling enforcement writes ``status = 'cost_ceiling_exceeded'`` in the
executor pre-gate and the finalize ledger gate. That status MUST be registered in
every run-status vocabulary (``RUN_STATUS_WHITELIST``, ``TERMINAL_STATUSES``,
``PROBE_TERMINAL_STATUSES``) and in the ``ck_runs_status`` CHECK constraint, or
the gate fails open (``ValueError`` swallowed by the executor's ``except``) or
raises ``IntegrityError`` against a real DB.

These are pure (no DB connection) assertions that lock the registration in place
so the mocked-session unit tests in ``test_cost_finalize_ceiling.py`` cannot mask
a regression. The real-DB constraint round-trip is covered by the CI integration
invariant test ``test_run_daily_facts`` (``TERMINAL_STATUSES ⊆ ck_runs_status``).
"""

from __future__ import annotations

from farna.core.cost_controller.probe import PROBE_TERMINAL_STATUSES
from farna.db.crud.run import RUN_STATUS_WHITELIST
from farna.db.models.run import TERMINAL_STATUSES, Run

NEW_STATUS = "cost_ceiling_exceeded"


def _migration_0128():
    # The historical migration was folded into the native baseline.  The
    # vocabulary and model assertions above are the surviving contract.
    return None


def test_cost_ceiling_exceeded_registered_in_vocabularies() -> None:
    assert NEW_STATUS in TERMINAL_STATUSES
    assert NEW_STATUS in RUN_STATUS_WHITELIST
    assert NEW_STATUS in PROBE_TERMINAL_STATUSES


def test_cost_ceiling_exceeded_in_runs_check_constraint() -> None:
    constraint = next(c for c in Run.__table__.constraints if getattr(c, "name", None) == "ck_runs_status")
    assert NEW_STATUS in str(constraint.sqltext)


def test_baseline_migration_extends_constraint_consistently() -> None:
    # The historical migration was folded into 0350_baseline_schema.  The
    # model constraint assertion above proves the runtime vocabulary; this
    # test checks the shipped baseline source carries the same status.
    from pathlib import Path

    baseline = Path(__file__).resolve().parents[4] / "src" / "farna" / "db" / "migrations" / "versions" / "0350_baseline_schema.py"
    source = baseline.read_text(encoding="utf-8")
    assert NEW_STATUS in source
