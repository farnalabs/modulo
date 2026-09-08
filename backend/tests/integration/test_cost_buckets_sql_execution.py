"""FAR-657 — execution coverage for the cost-bucket SQL statements.

The parity unit tests
(``tests/unit/core/cost_controller/test_cost_buckets_sql_aggregation.py``) pin
a hand-written Python transliteration of the two static statements inside
``build_cost_report_buckets`` against a MOCKED session — nothing executed the
actual SQL text, so a typo'd cast, alias, regex, or window predicate in those
literals would ship silently (empty buckets or a 503). This module runs the
REAL ``build_cost_report_buckets`` (and both statement literals directly)
against a migrated testcontainer Postgres over a representative run fixture,
pinning every output to a hand-computed literal.

The window anchors are date-immune: every in-window run carries a far-future
``started_at`` (>= ``:since`` for ANY period) and every out-of-window run a
far-past one (< ``:since`` for ANY period), so the org/period partition cannot
flake on a month boundary mid-test.

Requires Docker (testcontainers Postgres) — runs in CI's changed-integration
job; collection + style are verified locally.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.core.cost_controller import (
    _SQL_COST_COMPONENT_BUCKETS,
    _SQL_COST_LEGACY_TOTAL,
    _report_since,
    build_cost_report_buckets,
)
from modulo.db.crud.team import create_team

pytestmark = pytest.mark.integration

_STARTED_IN = datetime(2099, 1, 1, tzinfo=UTC)
_STARTED_OUT = datetime(2020, 1, 1, tzinfo=UTC)

# One run per fixture row: (team key "A"/"B"/None, total_cost_usd,
# cost_breakdown JSON text or None for SQL NULL, started_at). Every branch the
# parity fixture covers is represented — 6dp string amounts, JSON-number
# amounts, a marker run, legacy (NULL breakdown, both with and without a
# total), JSON-null breakdown (the legacy jsonb_typeof 'null' branch), the
# drop dispositions (non-dict entry, empty component, non-string component,
# malformed amount, boolean amount), the keep-as-0 disposition (null amount),
# plus the SQL-predicate-only branches: out-of-window, non-array breakdown,
# total_clamped FALSE (not a marker), and NULL started_at.
_RUN_FIXTURE: list[dict[str, object]] = [
    {  # (b) 6dp string amounts, two components
        "team": "A",
        "total": Decimal("1.750000"),
        "breakdown": '[{"component": "model", "amount_usd": "1.500000"}, '
        '{"component": "storage", "amount_usd": "0.250000"}]',
        "started": _STARTED_IN,
    },
    {  # same (team, component) as the first run — aggregates ACROSS runs
        "team": "A",
        "total": Decimal("0.500000"),
        "breakdown": '[{"component": "model", "amount_usd": "0.500000"}]',
        "started": _STARTED_IN,
    },
    {  # (a) JSON-number amount
        "team": "B",
        "total": Decimal("0.100000"),
        "breakdown": '[{"component": "tokens", "amount_usd": 1.25}]',
        "started": _STARTED_IN,
    },
    {  # negative 6dp string amount
        "team": "B",
        "total": Decimal("1.000000"),
        "breakdown": '[{"component": "credit", "amount_usd": "-0.250000"}]',
        "started": _STARTED_IN,
    },
    {  # no owner team — the __org__ unassigned bucket
        "team": None,
        "total": Decimal("0.750000"),
        "breakdown": '[{"component": "storage", "amount_usd": "0.750000"}]',
        "started": _STARTED_IN,
    },
    {  # (c) marker run — excluded wholesale (even its real component; the huge
        # total proves exclusion is by the marker, not by the amount)
        "team": "A",
        "total": Decimal("99999999.999999"),
        "breakdown": '[{"total_clamped": true, "amount_usd": "0.000000"}, '
        '{"component": "model", "amount_usd": "999.000000"}]',
        "started": _STARTED_IN,
    },
    {  # (d) legacy — SQL-NULL breakdown contributes its total
        "team": "A",
        "total": Decimal("2.000000"),
        "breakdown": None,
        "started": _STARTED_IN,
    },
    {  # legacy row with a NULL total — contributes nothing
        "team": "A",
        "total": None,
        "breakdown": None,
        "started": _STARTED_IN,
    },
    {  # (e) drop dispositions: non-dict entry, empty-string component,
        # non-string component, malformed amount, boolean amount — "junk" must
        # get NO bucket row at all (the HAVING drop)
        "team": "B",
        "total": Decimal("1.000000"),
        "breakdown": '["garbage", {"component": "", "amount_usd": "1.000000"}, '
        '{"component": 9, "amount_usd": "1.000000"}, '
        '{"component": "junk", "amount_usd": "not-a-number"}, '
        '{"component": "junk", "amount_usd": true}]',
        "started": _STARTED_IN,
    },
    {  # (f) null amount — KEPT as a 0.000000 row (jsonb_typeof 'null')
        "team": "B",
        "total": Decimal("1.000000"),
        "breakdown": '[{"component": "zero", "amount_usd": null}]',
        "started": _STARTED_IN,
    },
    {  # out-of-window — excluded from the component buckets AND the legacy total
        "team": "B",
        "total": Decimal("50.000000"),
        "breakdown": '[{"component": "model", "amount_usd": "50.000000"}]',
        "started": _STARTED_OUT,
    },
    {  # non-array (object) breakdown — neither legacy nor components
        "team": "B",
        "total": Decimal("5.000000"),
        "breakdown": '{"component": "model"}',
        "started": _STARTED_IN,
    },
    {  # total_clamped FALSE is NOT a marker — the run is included
        "team": "B",
        "total": Decimal("0.300000"),
        "breakdown": '[{"total_clamped": false, "component": "egress", "amount_usd": "0.300000"}]',
        "started": _STARTED_IN,
    },
    {  # NULL started_at — excluded from BOTH statements
        "team": "A",
        "total": Decimal("7.000000"),
        "breakdown": '[{"component": "ghost", "amount_usd": "7.000000"}]',
        "started": None,
    },
    {  # JSON-null breakdown — the legacy jsonb_typeof 'null' branch
        "team": "A",
        "total": Decimal("3.000000"),
        "breakdown": "null",
        "started": _STARTED_IN,
    },
]


async def test_cost_bucket_sql_statements_execute_and_match_expected_buckets(db_session: AsyncSession) -> None:
    """Both static statements execute against live Postgres and produce the
    hand-computed buckets; the wrapped function serializes them identically.

    Any typo in ``_SQL_COST_COMPONENT_BUCKETS`` / ``_SQL_COST_LEGACY_TOTAL``
    (wrong cast, alias, regex, marker comparison, or window predicate) changes
    one of the exact literals below — or raises — so the statements cannot
    regress to empty buckets or a 503 without this test failing.
    """
    org_id = uuid.uuid4()
    account_id = uuid.uuid4()

    await db_session.execute(
        text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
        {"id": str(org_id), "name": "Cost Bucket Org", "slug": f"cost-bucket-{org_id.hex[:8]}"},
    )
    await db_session.execute(
        text(
            "INSERT INTO accounts (id, email, display_name, password_hash, auth_provider, active) "
            "VALUES (:id, :email, :name, 'hash', 'local', true)"
        ),
        {
            "id": str(account_id),
            "email": f"cost-bucket-{account_id.hex[:8]}@integration.test",
            "name": "Cost Bucket User",
        },
    )
    await db_session.execute(
        text("SELECT set_config('app.organisation_id', :oid, true)"),
        {"oid": str(org_id)},
    )
    team_a = await create_team(db_session, org_id=org_id, name="Bucket Team A", account_id=account_id)
    team_b = await create_team(db_session, org_id=org_id, name="Bucket Team B", account_id=account_id)
    await db_session.flush()

    # pipeline_id/snapshot_id are NOT NULL FKs the statements never read — the
    # suite's established cost-controller pattern (crud/test_cost_attribution)
    # inserts runs under replica mode with dangling values for exactly that.
    await db_session.execute(text("SET session_replication_role = replica"))
    team_ids = {"A": str(team_a.id), "B": str(team_b.id), None: None}
    for run_number, row in enumerate(_RUN_FIXTURE, start=1):
        run_id = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, trigger_type, "
                "langgraph_thread_id, run_number, input_hash, owner_team_id, total_cost_usd, "
                "cost_breakdown, started_at) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', :thread, :num, :hash, :tid, :total, "
                ":breakdown, :started)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(uuid.uuid4()),
                "sid": str(uuid.uuid4()),
                "thread": f"cost-buckets-{run_id.hex}",
                "num": run_number,
                "hash": "0" * 64,
                "tid": team_ids[row["team"]],
                "total": row["total"],
                "breakdown": row["breakdown"],
                "started": row["started"],
            },
        )
    await db_session.execute(text("SET session_replication_role = DEFAULT"))

    # --- the raw statement literals execute directly ------------------------
    since = _report_since(datetime.now(UTC).date(), "month")
    component_rows = (
        await db_session.execute(text(_SQL_COST_COMPONENT_BUCKETS), {"org_id": org_id, "since": since})
    ).all()

    # exactly 7 GROUP rows: T1 {model, storage}, T2 {tokens, egress, zero,
    # credit}, org-unassigned {storage} — the dropped "junk" group must NOT
    # materialise, and none of the excluded runs may contribute.
    assert len(component_rows) == 7
    raw_buckets = {
        (str(row.team_id) if row.team_id is not None else "__org__", row.component): row.amount_usd
        for row in component_rows
    }
    assert raw_buckets == {
        (str(team_a.id), "model"): Decimal("2.000000"),
        (str(team_a.id), "storage"): Decimal("0.250000"),
        (str(team_b.id), "tokens"): Decimal("1.25"),
        (str(team_b.id), "egress"): Decimal("0.300000"),
        (str(team_b.id), "zero"): Decimal("0.000000"),
        (str(team_b.id), "credit"): Decimal("-0.250000"),
        ("__org__", "storage"): Decimal("0.750000"),
    }

    legacy_total = (
        await db_session.execute(text(_SQL_COST_LEGACY_TOTAL), {"org_id": org_id, "since": since})
    ).scalar_one()
    # run 7's 2.0 + run 15's 3.0 — NOT the out-of-window 50, the non-array 5,
    # the marker's 99999999.999999, or the NULL-started_at 7.
    assert legacy_total == Decimal("5.000000")

    # --- the real function over the same rows --------------------------------
    buckets = await build_cost_report_buckets(db_session, org_id=org_id, period="month")

    assert buckets["components_by_team"] == {
        str(team_a.id): [
            {"name": "model", "amount_usd": "2.000000"},
            {"name": "storage", "amount_usd": "0.250000"},
        ],
        str(team_b.id): [
            {"name": "tokens", "amount_usd": "1.250000"},
            {"name": "egress", "amount_usd": "0.300000"},
            {"name": "zero", "amount_usd": "0.000000"},
            {"name": "credit", "amount_usd": "-0.250000"},
        ],
        "__org__": [{"name": "storage", "amount_usd": "0.750000"}],
    }
    assert buckets["legacy_total"] == "5.000000"
    assert buckets["org_unassigned_components"] == "0.750000"
    # team components 2.25 + 1.30, org-unassigned 0.75, legacy 5.0
    assert buckets["org_total"] == "9.300000"
    assert buckets["has_more"] is False
    # no ledger rows were written for this fixture org
    assert not buckets["annotations_by_team"]
    assert buckets["org_run_count"] == 0
