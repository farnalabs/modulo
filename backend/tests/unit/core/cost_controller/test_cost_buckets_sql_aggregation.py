"""FAR-657 — SQL-side aggregation parity tests for ``build_cost_report_buckets``.

The costs-overview buckets were moved from "hydrate every run row and
aggregate per-component in Python" to Postgres jsonb aggregation
(``jsonb_array_elements`` lateral + GROUP BY + SUM). These tests pin two
invariants:

1. **Parity** — for a representative fixture of raw run rows, the new
   SQL-aggregated implementation produces buckets identical to the RETIRED
   Python algorithm (transcribed below as the reference). The SQL semantics
   are transliterated into Python by ``_sql_aggregate_rows`` /
   ``_sql_legacy_total``; a real-Postgres integration run is what proves the
   SQL text itself (unit mocks cannot), so the SQL is kept conservative:
   static text, bound params only, marker exclusion via JSON-boolean
   equality, and a HAVING that mirrors the retired drop-vs-keep entry
   dispositions.
2. **Bounded hydration** — every statement the function executes is a SQL
   aggregate; none returns unbounded per-run rows.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.cost_controller import (
    _REPORT_COMPONENT_LIMIT,
    build_cost_report_buckets,
)

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_TEAM_A = uuid.UUID("00000000-0000-0000-0000-00000000000a")
_TEAM_B = uuid.UUID("00000000-0000-0000-0000-00000000000b")
_TODAY = date(2026, 6, 24)
_FROZEN = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _freeze_datetime() -> Any:
    with patch("modulo.core.cost_controller.datetime") as mock_dt:
        mock_dt.now.return_value = _FROZEN
        mock_dt.UTC = UTC
        mock_dt.date = date
        mock_dt.timedelta = timedelta
        mock_dt.datetime = datetime
        yield mock_dt


# ---------------------------------------------------------------------------
# Reference — the RETIRED Python algorithm (transcribed from pre-FAR-657 main)
# ---------------------------------------------------------------------------


def _reference_buckets_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Runs-derived buckets exactly as the retired Python path computed them.

    Takes raw run rows ``(owner_team_id, total_cost_usd, cost_breakdown)``
    and returns the five runs-derived keys. The ledger-derived keys
    (``annotations_by_team``, ``org_run_count``) come from other queries and
    are out of scope.
    """
    from modulo.core.cost_controller import _report_amount

    team_components: dict[uuid.UUID | None, dict[str, Decimal]] = {}
    legacy_total = Decimal(0)

    for row in rows:
        team_id = row["owner_team_id"]
        breakdown = row["cost_breakdown"]
        if breakdown is None:
            try:
                if row["total_cost_usd"] is not None:
                    legacy_total += Decimal(str(row["total_cost_usd"]))
            except (TypeError, ValueError, ArithmeticError):
                continue
            continue
        if not isinstance(breakdown, list):
            continue
        if any(isinstance(e, dict) and e.get("total_clamped") is True for e in breakdown):
            continue
        bucket = team_components.setdefault(team_id, {})
        for entry in breakdown:
            if not isinstance(entry, dict):
                continue
            name = entry.get("component")
            if not isinstance(name, str) or not name:
                continue
            try:
                raw_amount = entry.get("amount_usd")
                amount = Decimal(str(raw_amount)) if raw_amount is not None else Decimal(0)
            except (TypeError, ValueError, ArithmeticError):
                continue
            bucket[name] = bucket.get(name, Decimal(0)) + amount

    def _serialized(
        bucket: dict[str, Decimal],
        limit: int = _REPORT_COMPONENT_LIMIT,
    ) -> tuple[list[dict[str, str]], bool]:
        entries = sorted(bucket.items(), key=lambda kv: (-kv[1], kv[0]))
        truncated = len(entries) > limit
        return [{"name": name, "amount_usd": _report_amount(amount)} for name, amount in entries[:limit]], truncated

    components_by_team: dict[str, list[dict[str, str]]] = {}
    has_more = False
    for team_id, bucket in team_components.items():
        key = str(team_id) if team_id is not None else "__org__"
        comps, truncated = _serialized(bucket)
        components_by_team[key] = comps
        if truncated:
            has_more = True

    org_unassigned = Decimal(0)
    team_sum = Decimal(0)
    for team_id, bucket in team_components.items():
        for amount in bucket.values():
            if team_id is None:
                org_unassigned += amount
            else:
                team_sum += amount

    return {
        "components_by_team": components_by_team,
        "legacy_total": _report_amount(legacy_total),
        "org_unassigned_components": _report_amount(org_unassigned),
        "org_total": _report_amount(team_sum + org_unassigned + legacy_total),
        "has_more": has_more,
    }


# ---------------------------------------------------------------------------
# SQL-semantics transliteration (what _SQL_COST_COMPONENT_BUCKETS computes)
# ---------------------------------------------------------------------------

_NUMERIC_STRING = re.compile(r"^\s*[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?\s*$")


def _sql_amount(amount: Any) -> Decimal:
    """The amount CASE: JSON numbers and numeric strings cast; everything else is 0."""
    if isinstance(amount, bool):
        return Decimal(0)
    if isinstance(amount, (int, float)):
        return Decimal(str(amount))
    if isinstance(amount, str) and _NUMERIC_STRING.match(amount):
        return Decimal(amount)
    return Decimal(0)


def _sql_amount_parseable(amount: Any) -> bool:
    """The HAVING parse-keeps-row test: number, numeric string, or null/absent."""
    if amount is None:
        return True
    if isinstance(amount, bool):
        return False
    if isinstance(amount, (int, float)):
        return True
    return isinstance(amount, str) and bool(_NUMERIC_STRING.match(amount))


def _is_marker_breakdown(breakdown: Any) -> bool:
    if not isinstance(breakdown, list):
        return False
    return any(isinstance(e, dict) and e.get("total_clamped") is True for e in breakdown)


def _sql_aggregate_rows(rows: list[dict[str, Any]]) -> list[SimpleNamespace]:
    """Transliteration of ``_SQL_COST_COMPONENT_BUCKETS`` over raw fixture rows."""
    grouped: dict[tuple[uuid.UUID | None, str], Decimal] = {}
    parse_kept: dict[tuple[uuid.UUID | None, str], int] = {}
    for row in rows:
        breakdown = row["cost_breakdown"]
        if breakdown is None or not isinstance(breakdown, list):
            continue
        if _is_marker_breakdown(breakdown):
            continue
        team_id = row["owner_team_id"]
        for elem in breakdown:
            if not isinstance(elem, dict):
                continue
            name = elem.get("component")
            if not isinstance(name, str) or not name:
                continue
            key = (team_id, name)
            grouped[key] = grouped.get(key, Decimal(0)) + _sql_amount(elem.get("amount_usd"))
            if _sql_amount_parseable(elem.get("amount_usd")):
                parse_kept[key] = parse_kept.get(key, 0) + 1
    return [
        SimpleNamespace(team_id=team_id, component=name, amount_usd=total)
        for (team_id, name), total in grouped.items()
        if parse_kept.get((team_id, name), 0) > 0
    ]


def _sql_legacy_total(rows: list[dict[str, Any]]) -> Decimal:
    """Transliteration of ``_SQL_COST_LEGACY_TOTAL`` over raw fixture rows."""
    total = Decimal(0)
    for row in rows:
        if row["total_cost_usd"] is None:
            continue
        if row["cost_breakdown"] is None:
            total += Decimal(str(row["total_cost_usd"]))
    return total


# ---------------------------------------------------------------------------
# Session mock — the 4-query execute order
# ---------------------------------------------------------------------------


def _mock_session(
    component_rows: list[SimpleNamespace],
    legacy_total: Decimal,
    *,
    annotations: list[Any] | None = None,
    org_run_count: Any = None,
) -> AsyncMock:
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[
            MagicMock(all=MagicMock(return_value=component_rows)),  # component buckets
            MagicMock(scalar_one=MagicMock(return_value=legacy_total)),  # legacy total
            MagicMock(all=MagicMock(return_value=annotations or [])),  # ledger annotations
            MagicMock(scalar_one_or_none=MagicMock(return_value=org_run_count)),  # org run count
        ]
    )
    return session


def _representative_rows() -> list[dict[str, Any]]:
    """A fixture exercising every retired-algorithm branch."""
    return [
        {
            "owner_team_id": _TEAM_A,
            "total_cost_usd": Decimal("1.750000"),
            "cost_breakdown": [
                {"component": "model", "amount_usd": "1.500000"},
                {"component": "storage", "amount_usd": "0.250000"},
            ],
        },
        {  # same team + component as the first run — aggregates across runs
            "owner_team_id": _TEAM_A,
            "total_cost_usd": Decimal("0.500000"),
            "cost_breakdown": [{"component": "model", "amount_usd": "0.500000"}],
        },
        {
            "owner_team_id": _TEAM_B,
            "total_cost_usd": Decimal("0.100000"),
            "cost_breakdown": [{"component": "model", "amount_usd": "0.100000"}],
        },
        {  # no team — org-unassigned bucket
            "owner_team_id": None,
            "total_cost_usd": Decimal("0.750000"),
            "cost_breakdown": [{"component": "storage", "amount_usd": "0.750000"}],
        },
        {  # legacy — no breakdown at all
            "owner_team_id": _TEAM_A,
            "total_cost_usd": Decimal("2.000000"),
            "cost_breakdown": None,
        },
        {  # legacy with a NULL total — contributes nothing
            "owner_team_id": _TEAM_A,
            "total_cost_usd": None,
            "cost_breakdown": None,
        },
        {  # marker run — excluded wholesale (even its other components)
            "owner_team_id": _TEAM_A,
            "total_cost_usd": Decimal("99999999.999999"),
            "cost_breakdown": [
                {"total_clamped": True, "amount_usd": "0.000000"},
                {"component": "model", "amount_usd": "999.000000"},
            ],
        },
        {  # total_clamped FALSE is NOT a marker
            "owner_team_id": _TEAM_B,
            "total_cost_usd": Decimal("0.300000"),
            "cost_breakdown": [{"total_clamped": False, "component": "egress", "amount_usd": "0.300000"}],
        },
        {  # non-list breakdown — skipped entirely (neither legacy nor components)
            "owner_team_id": _TEAM_B,
            "total_cost_usd": Decimal("5.000000"),
            "cost_breakdown": {"component": "model"},
        },
        {  # malformed entries — every drop branch
            "owner_team_id": _TEAM_B,
            "total_cost_usd": Decimal("1.000000"),
            "cost_breakdown": [
                "garbage",
                7,
                {"amount_usd": "1.000000"},
                {"component": "", "amount_usd": "1.000000"},
                {"component": 9, "amount_usd": "1.000000"},
                {"component": "junk", "amount_usd": "not-a-number"},
                {"component": "junk", "amount_usd": True},
                {"component": "zero", "amount_usd": None},
            ],
        },
        {  # JSON-number amount and a negative amount
            "owner_team_id": _TEAM_B,
            "total_cost_usd": Decimal("1.000000"),
            "cost_breakdown": [
                {"component": "tokens", "amount_usd": 1.25},
                {"component": "credit", "amount_usd": "-0.250000"},
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Parity — new SQL-aggregated buckets == retired Python algorithm
# ---------------------------------------------------------------------------


async def test_buckets_match_retired_python_algorithm_on_representative_fixture() -> None:
    rows = _representative_rows()
    session = _mock_session(_sql_aggregate_rows(rows), _sql_legacy_total(rows))

    buckets = await build_cost_report_buckets(session, org_id=_ORG_ID, period="month")

    expected = _reference_buckets_from_rows(rows)
    runs_derived_keys = ("components_by_team", "legacy_total", "org_unassigned_components", "org_total", "has_more")
    for key in runs_derived_keys:
        assert buckets[key] == expected[key], f"key {key} drifted from the retired algorithm"


def test_fixture_covers_the_distinguishing_branches() -> None:
    """The representative fixture really exercises marker/legacy/unassigned/junk paths."""
    buckets = _reference_buckets_from_rows(_representative_rows())
    team_a_key = str(_TEAM_A)
    team_b_key = str(_TEAM_B)
    assert buckets["components_by_team"][team_a_key] == [
        {"name": "model", "amount_usd": "2.000000"},
        {"name": "storage", "amount_usd": "0.250000"},
    ]
    assert buckets["components_by_team"][team_b_key] == [
        {"name": "tokens", "amount_usd": "1.250000"},
        {"name": "egress", "amount_usd": "0.300000"},
        {"name": "model", "amount_usd": "0.100000"},
        {"name": "zero", "amount_usd": "0.000000"},
        {"name": "credit", "amount_usd": "-0.250000"},
    ]
    assert buckets["components_by_team"]["__org__"] == [{"name": "storage", "amount_usd": "0.750000"}]
    assert buckets["legacy_total"] == "2.000000"
    assert buckets["org_unassigned_components"] == "0.750000"
    # team_a 2.0 + 0.25, team_b 1.25 + 0.30 + 0.10 + 0.0 - 0.25, org 0.75, legacy 2.0
    assert buckets["org_total"] == "6.400000"
    assert buckets["has_more"] is False


async def test_malformed_amount_component_is_dropped_not_zero_row() -> None:
    """A component whose amounts ALL fail to parse gets no bucket row at all.

    The retired path emitted an empty ``[]`` bucket for the team; the SQL path
    has no GROUP row to build one, so the team key is ABSENT — the route
    renders both identically via ``components_by_team.get(key, [])``.
    """
    rows = [
        {
            "owner_team_id": _TEAM_A,
            "total_cost_usd": Decimal("1.000000"),
            "cost_breakdown": [
                {"component": "junk", "amount_usd": "not-a-number"},
                {"component": "junk", "amount_usd": True},
            ],
        },
        {
            "owner_team_id": _TEAM_B,
            "total_cost_usd": Decimal("1.000000"),
            "cost_breakdown": [{"component": "model", "amount_usd": "1.000000"}],
        },
    ]
    session = _mock_session(_sql_aggregate_rows(rows), _sql_legacy_total(rows))

    buckets = await build_cost_report_buckets(session, org_id=_ORG_ID, period="month")

    assert str(_TEAM_A) not in buckets["components_by_team"]
    assert buckets["components_by_team"][str(_TEAM_B)] == [{"name": "model", "amount_usd": "1.000000"}]


async def test_none_amount_component_is_kept_as_zero_row() -> None:
    """A component with a null amount KEPT its bucket row (0.000000) in the retired path."""
    rows = [
        {
            "owner_team_id": _TEAM_A,
            "total_cost_usd": Decimal("1.000000"),
            "cost_breakdown": [{"component": "zero", "amount_usd": None}],
        }
    ]
    session = _mock_session(_sql_aggregate_rows(rows), _sql_legacy_total(rows))

    buckets = await build_cost_report_buckets(session, org_id=_ORG_ID, period="month")

    assert buckets["components_by_team"][str(_TEAM_A)] == [{"name": "zero", "amount_usd": "0.000000"}]


async def test_components_sorted_by_amount_desc_then_name() -> None:
    rows = [
        {
            "owner_team_id": _TEAM_A,
            "total_cost_usd": None,
            "cost_breakdown": [
                {"component": "zeta", "amount_usd": "3.000000"},
                {"component": "alpha", "amount_usd": "3.000000"},
                {"component": "mid", "amount_usd": "9.000000"},
            ],
        }
    ]
    session = _mock_session(_sql_aggregate_rows(rows), _sql_legacy_total(rows))

    buckets = await build_cost_report_buckets(session, org_id=_ORG_ID, period="month")

    names = [entry["name"] for entry in buckets["components_by_team"][str(_TEAM_A)]]
    assert names == ["mid", "alpha", "zeta"]


async def test_truncation_at_report_component_limit_sets_has_more() -> None:
    count = _REPORT_COMPONENT_LIMIT + 1
    rows = [
        {
            "owner_team_id": _TEAM_A,
            "total_cost_usd": None,
            "cost_breakdown": [{"component": f"c{i:04d}", "amount_usd": f"{i}.000000"} for i in range(count)],
        }
    ]
    component_rows = _sql_aggregate_rows(rows)
    assert len(component_rows) == count
    session = _mock_session(component_rows, _sql_legacy_total(rows))

    buckets = await build_cost_report_buckets(session, org_id=_ORG_ID, period="month")

    serialized = buckets["components_by_team"][str(_TEAM_A)]
    assert len(serialized) == _REPORT_COMPONENT_LIMIT
    assert serialized[0]["name"] == "c0500"
    assert buckets["has_more"] is True


async def test_period_validation_still_rejects_unknown_period() -> None:
    session = _mock_session([], Decimal(0))
    with pytest.raises(ValueError, match="Unknown period"):
        await build_cost_report_buckets(session, org_id=_ORG_ID, period="century")
    session.execute.assert_not_awaited()


async def test_full_response_shape_with_ledger_annotations() -> None:
    """The ledger-derived keys flow through unchanged alongside the SQL buckets."""
    rows = _representative_rows()
    annotations = [
        SimpleNamespace(team_id=_TEAM_A, refused_total=Decimal("0.500000"), clamped_total=Decimal("1.000000")),
        SimpleNamespace(team_id=None, refused_total=Decimal("0.000000"), clamped_total=Decimal("0.000000")),
    ]
    session = _mock_session(
        _sql_aggregate_rows(rows),
        _sql_legacy_total(rows),
        annotations=annotations,
        org_run_count=Decimal(42),
    )

    buckets = await build_cost_report_buckets(session, org_id=_ORG_ID, period="month")

    assert buckets["annotations_by_team"][str(_TEAM_A)] == {
        "refused_total_usd": 0.5,
        "clamped_total_usd": 1.0,
    }
    assert buckets["annotations_by_team"]["__org__"] == {"refused_total_usd": None, "clamped_total_usd": None}
    assert buckets["org_run_count"] == 42
    assert isinstance(buckets["org_total"], str)


# ---------------------------------------------------------------------------
# Regression — bounded hydration (no unbounded per-row reads)
# ---------------------------------------------------------------------------


async def test_every_executed_statement_is_a_sql_aggregate() -> None:
    """No statement returns unbounded per-run rows: each is a SUM aggregate.

    Guards the FAR-657 fix against regressing to the retired path, which
    selected (owner_team_id, total_cost_usd, cost_breakdown) for EVERY run in
    the period and aggregated in Python.
    """
    rows = _representative_rows()
    session = _mock_session(_sql_aggregate_rows(rows), _sql_legacy_total(rows))

    await build_cost_report_buckets(session, org_id=_ORG_ID, period="month")

    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert len(statements) == 4
    for sql in statements:
        assert "sum(" in sql.lower(), f"statement is not a SQL aggregate:\n{sql}"


async def test_component_buckets_statement_expands_and_groups_in_sql() -> None:
    """The buckets statement expands + groups the breakdown IN POSTGRES."""
    rows = _representative_rows()
    session = _mock_session(_sql_aggregate_rows(rows), _sql_legacy_total(rows))

    await build_cost_report_buckets(session, org_id=_ORG_ID, period="month")

    components_sql = str(session.execute.await_args_list[0].args[0])
    assert "jsonb_array_elements" in components_sql
    assert "GROUP BY r.owner_team_id, elem->>'component'" in components_sql
    assert "total_clamped" in components_sql
    assert ":org_id" in components_sql
    assert ":since" in components_sql
    # self-contained fragment: only the runs table is named in its own FROM
    assert "FROM runs" in components_sql
    assert "org_daily_run_counts" not in components_sql


async def test_legacy_statement_is_a_scoped_sum() -> None:
    rows = _representative_rows()
    session = _mock_session(_sql_aggregate_rows(rows), _sql_legacy_total(rows))

    await build_cost_report_buckets(session, org_id=_ORG_ID, period="month")

    legacy_sql = str(session.execute.await_args_list[1].args[0])
    assert "COALESCE(SUM(total_cost_usd), 0)" in legacy_sql
    assert "FROM runs" in legacy_sql
    assert "jsonb_typeof" in legacy_sql


async def test_numeric_string_regex_is_embedded_in_the_buckets_sql() -> None:
    """The SQL embeds the SAME numeric-string pattern the parity simulator uses.

    The SQL is a static literal (no interpolation), so the pattern is written
    inline twice (amount CASE + HAVING). This pins the embedded pattern to the
    documented constant so the SQL cannot drift from the parity semantics.
    """
    from modulo.core.cost_controller import _NUMERIC_STRING_RE

    session = _mock_session([], Decimal(0))
    await build_cost_report_buckets(session, org_id=_ORG_ID, period="month")

    components_sql = str(session.execute.await_args_list[0].args[0])
    occurrences = components_sql.count(_NUMERIC_STRING_RE)
    assert occurrences == 2


async def test_queries_are_scoped_to_org_and_period() -> None:
    """Both new statements bind the org + period predicate (RLS-independent scoping)."""
    rows = _representative_rows()
    session = _mock_session(_sql_aggregate_rows(rows), _sql_legacy_total(rows))

    await build_cost_report_buckets(session, org_id=_ORG_ID, period="month")

    components_params = session.execute.await_args_list[0].args[1]
    legacy_params = session.execute.await_args_list[1].args[1]
    assert components_params["org_id"] == _ORG_ID
    assert components_params["since"] == date(2026, 6, 1)
    assert legacy_params["org_id"] == _ORG_ID
    assert legacy_params["since"] == date(2026, 6, 1)
