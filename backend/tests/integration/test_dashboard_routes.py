"""Container-tier coverage for the dashboard routes (FAR-1721).

The dashboard's existing evidence drives ``GET /api/v1/dashboard/summary``,
``/trends`` and ``/daily-run-counts`` through mocked sessions — every
aggregation query is answered by a canned row set, so the SQL itself (org
scoping, the ``org_level_predicate()`` ledger filter, the pending/claimed →
``idle`` fold, the eval join, the ``?days=`` period windows) never executes
against a database.

This module runs the same routes through the ASGI app against a
testcontainers Postgres with seeded rows. Assertions that only a real
database can make:

* the summary/trends 7-day run-count and spend trend must sum ONLY the
  org-level ledger row (``team_id IS NULL``) — a team row for the same date
  must not double the org figure;
* ``?days=`` period windows must be computed from ``run_daily_facts.run_date``
  day buckets with an exclusive end (``[today - days, today)``), current vs
  previous compared over equal-length windows;
* the ``idle`` fold, per-team metrics, eval pass-rate join, recent-runs
  projection and cross-org isolation must survive the real SQL and RLS.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from modulo.auth.jwt import create_access_token

pytestmark = pytest.mark.integration

_VALID_32 = "a" * 32


def _token(org_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return create_access_token(
        subject=f"user-{user_id.hex[:8]}",
        secret_key=_VALID_32,
        organisation_id=str(org_id),
        account_id=str(user_id),
        org_role="admin",
        is_system_admin=False,
        client_kind="browser",
    )


async def _get(client: AsyncClient, org_id: uuid.UUID, user_id: uuid.UUID, url: str) -> dict:
    resp = await client.get(url, headers={"Authorization": f"Bearer {_token(org_id, user_id)}"})
    assert resp.status_code == 200, f"GET {url} -> {resp.status_code}: {resp.text}"
    return resp.json()


# ---------------------------------------------------------------------------
# Seed helpers (superuser engine, same raw-INSERT convention as the sibling
# integration modules — the app-engine route sessions read these under RLS).
# ---------------------------------------------------------------------------


async def _seed_org(engine: AsyncEngine, label: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO organisations (id, name, slug, settings_json) VALUES (:id, :name, :slug, '{}'::json)"),
            {"id": str(org_id), "name": f"Dash {label}", "slug": f"dash-{label}-{org_id.hex[:8]}"},
        )
    return org_id


async def _seed_user(engine: AsyncEngine, org_id: uuid.UUID, label: str) -> uuid.UUID:
    account_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO accounts (id, email, display_name, auth_provider, active, password_hash) "
                "VALUES (:id, :email, :name, 'local', true, 'hash')"
            ),
            {"id": str(account_id), "email": f"dash-{label}-{org_id.hex[:8]}@test.local", "name": f"Dash {label}"},
        )
        await conn.execute(
            text(
                "INSERT INTO org_memberships (id, account_id, organisation_id, role) VALUES (:mid, :aid, :oid, 'admin')"
            ),
            {"mid": str(uuid.uuid4()), "aid": str(account_id), "oid": str(org_id)},
        )
    return account_id


async def _seed_pipeline(engine: AsyncEngine, org_id: uuid.UUID, user_id: uuid.UUID, label: str) -> uuid.UUID:
    pipeline_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipelines (id, organisation_id, name, account_id, "
                "max_concurrent_runs, lock_wait_timeout_seconds, node_timeout_seconds, "
                "run_context_defaults, graph_nodes_json, default_autonomy_level, visibility) "
                "VALUES (:id, :oid, :name, :uid, 10, 30, 300, '{}'::json, '[]'::json, "
                "'manual_approval', 'org')"
            ),
            {"id": str(pipeline_id), "oid": str(org_id), "name": f"Dash Pipeline {label}", "uid": str(user_id)},
        )
    return pipeline_id


async def _seed_snapshot(engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID) -> uuid.UUID:
    snapshot_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (id, pipeline_id, organisation_id, "
                "snapshot_version, graph_json, connector_bindings_json, schema_pins_json, "
                "prompt_pins_json, model_backend_pins_json, run_context_defaults, config_json) "
                "VALUES (:id, :pid, :oid, 1, '[]'::json, '[]'::json, '[]'::json, '[]'::json, "
                "'[]'::json, '{}'::json, '{}'::json)"
            ),
            {"id": str(snapshot_id), "pid": str(pipeline_id), "oid": str(org_id)},
        )
    return snapshot_id


async def _seed_team(engine: AsyncEngine, org_id: uuid.UUID, user_id: uuid.UUID, label: str) -> uuid.UUID:
    team_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO teams (id, organisation_id, name, account_id, "
                "notification_endpoints, settings) "
                "VALUES (:id, :oid, :name, :aid, '[]'::json, '{}'::json)"
            ),
            {"id": str(team_id), "oid": str(org_id), "name": f"Dash Team {label}", "aid": str(user_id)},
        )
    return team_id


async def _seed_run(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    *,
    status: str,
    run_number: int,
    owner_team_id: uuid.UUID | None = None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, owner_team_id, "
                "trigger_type, input_hash, langgraph_thread_id, run_number, status) "
                "VALUES (:id, :oid, :pid, :sid, :tid, 'manual', :ih, :thread, :rn, :st)"
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "tid": str(owner_team_id) if owner_team_id else None,
                "ih": uuid.uuid4().hex + uuid.uuid4().hex[:32],
                "thread": f"{org_id}:{run_id}",
                "rn": run_number,
                "st": status,
            },
        )
    return run_id


async def _seed_fact(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    run_date: date,
    *,
    status: str,
    pipeline_id: uuid.UUID,
    total_tokens: int | None = None,
    duration_ms: int | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO run_daily_facts (id, organisation_id, run_id, run_date, trigger_type, "
                "status, pipeline_id, total_tokens, duration_ms) "
                "VALUES (:id, :oid, :rid, :day, 'manual', :st, :pid, :tok, :dur)"
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "rid": str(run_id),
                "day": run_date,
                "st": status,
                "pid": str(pipeline_id),
                "tok": total_tokens,
                "dur": duration_ms,
            },
        )


async def _seed_ledger(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    run_date: date,
    *,
    run_count: int,
    spend_usd: float,
    team_id: uuid.UUID | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO org_daily_run_counts (id, organisation_id, team_id, run_date, "
                "run_count, total_spend_usd) VALUES (:id, :oid, :tid, :d, :rc, :spend)"
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "tid": str(team_id) if team_id else None,
                "d": run_date,
                "rc": run_count,
                "spend": spend_usd,
            },
        )


async def _seed_eval(engine: AsyncEngine, org_id: uuid.UUID, pipeline_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID:
    eval_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO evals (id, organisation_id, pipeline_id, node_id, name, "
                "eval_type, config_json, account_id) "
                "VALUES (:id, :oid, :pid, NULL, 'Dash Eval', 'llm_judge', '{}'::jsonb, :aid)"
            ),
            {"id": str(eval_id), "oid": str(org_id), "pid": str(pipeline_id), "aid": str(user_id)},
        )
    return eval_id


async def _seed_eval_result(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    eval_id: uuid.UUID,
    *,
    passed: bool,
    evaluated_at: datetime | None = None,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO eval_results (id, organisation_id, run_id, eval_id, passed, score, evaluated_at) "
                "VALUES (:id, :oid, :rid, :eid, :passed, :score, COALESCE(:at, now()))"
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "rid": str(run_id),
                "eid": str(eval_id),
                "passed": passed,
                "score": 1.0 if passed else 0.0,
                "at": evaluated_at,
            },
        )


async def _seed_hitl_decision(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    review_id: str,
    decision: str,
    created_at: datetime,
    decision_at: datetime,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO hitl_claims (id, organisation_id, run_id, pipeline_id, review_id, "
                "account_id, claimed_at, claim_token, expires_at, gate_config_json, created_at, "
                "decision, decision_at) "
                "VALUES (:id, :oid, :rid, :pid, :review, :aid, :claimed, :tok, :exp, "
                "'{}'::json, :created, :decision, :decision_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "rid": str(run_id),
                "pid": str(pipeline_id),
                "review": review_id,
                "aid": str(user_id),
                "claimed": created_at,
                "tok": f"tok-{review_id}",
                "exp": created_at + timedelta(minutes=15),
                "created": created_at,
                "decision": decision,
                "decision_at": decision_at,
            },
        )


async def _seed_feedback(
    engine: AsyncEngine,
    org_id: uuid.UUID,
    run_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    created_at: datetime,
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO feedback_records (id, organisation_id, run_id, review_id, account_id, "
                "rejection_reason, rejected_output, producing_node_id, feedback_status, "
                "feedback_handler_type, created_at) "
                "VALUES (:id, :oid, :rid, 'dash-review', :aid, 'flagged', "
                "'{\"body\": \"seed\"}'::json, :nid, 'resolved', 'human', :created)"
            ),
            {
                "id": str(uuid.uuid4()),
                "oid": str(org_id),
                "rid": str(run_id),
                "aid": str(user_id),
                "nid": str(uuid.uuid4()),
                "created": created_at,
            },
        )


# ---------------------------------------------------------------------------
# GET /api/v1/dashboard/summary
# ---------------------------------------------------------------------------


class TestSummary:
    async def test_counts_status_fold_teams_and_evals(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        """Seeded rows flow through the real aggregation SQL end to end.

        Covers: per-status counts with the pending/claimed -> idle fold, the
        ``total_runs`` single-count invariant, active-pipeline count, the
        per-team breakdown with its eval enrichment, the eval pass-rate join,
        the 7-day trend projection, recent runs, and the no-model-backend
        config warning — all read by the route under RLS.
        """
        org_id = await _seed_org(db_engine, "summary")
        user_id = await _seed_user(db_engine, org_id, "summary")
        pipeline_id = await _seed_pipeline(db_engine, org_id, user_id, "summary")
        snapshot_id = await _seed_snapshot(db_engine, org_id, pipeline_id)
        team_id = await _seed_team(db_engine, org_id, user_id, "summary")

        statuses = ("complete", "complete", "failed", "pending", "claimed")
        run_ids: list[uuid.UUID] = []
        for i, status in enumerate(statuses, start=1):
            run_ids.append(
                await _seed_run(
                    db_engine,
                    org_id,
                    pipeline_id,
                    snapshot_id,
                    status=status,
                    run_number=i,
                    owner_team_id=team_id,
                )
            )

        eval_id = await _seed_eval(db_engine, org_id, pipeline_id, user_id)
        await _seed_eval_result(db_engine, org_id, run_ids[0], eval_id, passed=True)
        await _seed_eval_result(db_engine, org_id, run_ids[2], eval_id, passed=False)

        payload = await _get(integration_client, org_id, user_id, "/api/v1/dashboard/summary")

        assert payload["total_runs"] == 5, f"total_runs must count every seeded run once: {payload}"
        counts = payload["run_counts_by_status"]
        assert counts["complete"] == 2, f"complete runs missing from status counts: {counts}"
        assert counts["failed"] == 1, f"failed run missing from status counts: {counts}"
        assert counts["pending"] == 1, f"pending run missing from status counts: {counts}"
        assert counts["claimed"] == 1, f"claimed run missing from status counts: {counts}"
        assert counts["idle"] == 2, f"pending+claimed must fold into idle: {counts}"
        assert counts["running"] == 0, f"tracked statuses are zero-filled when absent: {counts}"
        assert payload["active_pipelines"] == 1, f"expected one active pipeline: {payload['active_pipelines']}"

        teams = payload["teams"]
        assert len(teams) == 1, f"expected exactly the seeded team: {teams}"
        team = teams[0]
        assert team["name"] == "Dash Team summary", f"team name must round-trip: {team}"
        assert team["run_counts_by_status"]["failed"] == 1, f"team breakdown lost the failed run: {team}"
        assert team["run_counts_by_status"]["idle"] == 2, f"team breakdown lost the idle fold: {team}"

        eval_rate = payload["eval_pass_rate"]
        assert eval_rate is not None, "seeded eval results must produce a pass-rate block"
        assert eval_rate["overall_pass_rate"] == 50.0, f"expected 1 of 2 evals to pass: {eval_rate}"
        assert eval_rate["total_evals"] == 2, f"both eval results must count: {eval_rate}"
        assert eval_rate["passed_evals"] == 1, f"exactly one eval passed: {eval_rate}"
        per_pipeline = eval_rate["per_pipeline"]
        assert str(pipeline_id) in per_pipeline, f"per-pipeline eval stats missing: {per_pipeline}"
        assert per_pipeline[str(pipeline_id)]["pass_rate"] == 50.0, f"per-pipeline pass rate wrong: {per_pipeline}"
        team_eval = team.get("eval_pass_rate")
        assert team_eval is not None, "team metrics must carry the eval enrichment"
        assert team_eval["total_evals"] == 2, f"team eval total wrong: {team_eval}"
        assert team_eval["pass_rate"] == 50.0, f"team eval pass rate wrong: {team_eval}"

        today = datetime.now(UTC).date()
        trend = payload["trend"]
        assert len(trend) == 7, f"trend must project exactly 7 days: {trend}"
        assert trend[0]["date"] == (today - timedelta(days=6)).isoformat(), f"trend must start 6 days back: {trend[0]}"
        assert trend[-1]["date"] == today.isoformat(), f"trend must end today: {trend[-1]}"
        assert {entry["run_count"] for entry in trend} == {0}, "an org with no ledger rows trends zero runs"

        recent_ids = {entry["id"] for entry in payload["recent_runs"]}
        assert recent_ids == {str(rid) for rid in run_ids}, f"recent_runs must surface every seeded run: {recent_ids}"
        recent_statuses = {entry["status"] for entry in payload["recent_runs"]}
        assert "failed" in recent_statuses, f"recent_runs must carry run status: {payload['recent_runs']}"

        warning_types = [w["type"] for w in payload["config_warnings"]]
        assert warning_types == ["no_model_backends"], f"fresh org must warn about missing providers: {warning_types}"

        assert "period" not in payload, "the period block must be absent without ?days="

    async def test_trend_sums_only_the_org_level_ledger_row(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        """A team row for the same date must not double the org trend figure.

        ``org_daily_run_counts`` carries an org-level row (``team_id IS NULL``)
        that ALREADY includes team-owned runs, plus a per-team breakdown row.
        The 7-day trend must sum only the org row — a mocked session cannot
        expose the double-count, so this runs against real Postgres.
        """
        org_id = await _seed_org(db_engine, "trendledger")
        user_id = await _seed_user(db_engine, org_id, "trendledger")
        team_id = await _seed_team(db_engine, org_id, user_id, "trendledger")
        today = datetime.now(UTC).date()
        await _seed_ledger(db_engine, org_id, today, run_count=3, spend_usd=30.0)
        await _seed_ledger(db_engine, org_id, today, run_count=2, spend_usd=20.0, team_id=team_id)

        payload = await _get(integration_client, org_id, user_id, "/api/v1/dashboard/summary")

        entry = next((e for e in payload["trend"] if e["date"] == today.isoformat()), None)
        assert entry is not None, f"today's trend entry missing: {payload['trend']}"
        assert entry["run_count"] == 3, (
            "org-level trend run_count must sum ONLY the team_id IS NULL row — the team row "
            f"already rolled into it (got {entry['run_count']}, expected 3)"
        )
        assert entry["token_spend_usd"] == 30.0, (
            "org-level trend spend must sum ONLY the team_id IS NULL row "
            f"(got {entry['token_spend_usd']}, expected 30.0)"
        )

    async def test_period_block_computes_windows_from_run_date_buckets(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        """``?days=7`` compares equal-length windows over ``run_daily_facts``.

        The current window is ``[today - 7, today)`` (end exclusive — today's
        own fact sits outside it); the previous window is the equal-length
        period before it. Value and delta come from the same source.
        """
        org_id = await _seed_org(db_engine, "period")
        user_id = await _seed_user(db_engine, org_id, "period")
        pipeline_id = await _seed_pipeline(db_engine, org_id, user_id, "period")
        today = datetime.now(UTC).date()

        await _seed_fact(
            db_engine,
            org_id,
            uuid.uuid4(),
            today - timedelta(days=1),
            status="complete",
            pipeline_id=pipeline_id,
            total_tokens=100,
            duration_ms=1000,
        )
        await _seed_fact(
            db_engine,
            org_id,
            uuid.uuid4(),
            today - timedelta(days=10),
            status="failed",
            pipeline_id=pipeline_id,
            total_tokens=50,
            duration_ms=500,
        )

        payload = await _get(integration_client, org_id, user_id, "/api/v1/dashboard/summary?days=7")

        period = payload["period"]
        assert period["days"] == 7, f"period block must echo the requested window: {period}"
        metrics = period["metrics"]

        total_runs = metrics["total_runs"]
        assert total_runs["current"] == 1, f"current window must hold exactly the today-1 fact: {total_runs}"
        assert total_runs["previous"] == 1, f"previous window must hold exactly the today-10 fact: {total_runs}"
        assert total_runs["delta_pct"] == 0.0, f"equal windows must trend 0%: {total_runs}"

        tokens = metrics["tokens"]
        assert tokens["current"] == 100, f"current-window tokens wrong: {tokens}"
        assert tokens["previous"] == 50, f"previous-window tokens wrong: {tokens}"
        assert tokens["delta_pct"] == 100.0, f"token delta must compare same-source windows: {tokens}"

        success = metrics["success_rate"]
        assert success["current"] == 100.0, f"current window's only fact completed: {success}"
        assert success["previous"] == 0.0, f"previous window's only fact failed: {success}"
        assert success["delta_pct"] is None, f"zero baseline has no defined delta: {success}"

        failed_status = metrics["run_counts_by_status"]["failed"]
        assert failed_status["current"] == 0, f"current window holds no failed fact: {failed_status}"
        assert failed_status["previous"] == 1, f"previous window holds the failed fact: {failed_status}"
        assert failed_status["delta_pct"] == -100.0, f"1 -> 0 is a 100% drop: {failed_status}"

        assert metrics["eval_pass_rate"]["current"] is None, "no eval results seeded — pass rate is undefined"
        spend = metrics["spend"]
        assert spend["current"] == 0.0, f"no ledger rows seeded — spend is zero: {spend}"
        assert spend["delta_pct"] is None, f"zero spend baseline has no delta: {spend}"

    async def test_days_outside_the_1_to_90_bound_returns_422(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        org_id = await _seed_org(db_engine, "daysbound")
        user_id = await _seed_user(db_engine, org_id, "daysbound")
        token = _token(org_id, user_id)

        for days in (0, 91):
            resp = await integration_client.get(
                f"/api/v1/dashboard/summary?days={days}",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 422, f"?days={days} must fail the 1..90 bound: {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# GET /api/v1/dashboard/trends
# ---------------------------------------------------------------------------


class TestTrends:
    async def test_series_reflect_seeded_rows(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        """The trend series must be computed from real ledger/eval/HITL/feedback rows.

        Same org-level ledger invariant as the summary test: the seeded team
        row for today must NOT be added into ``run_counts``/``token_spend``.
        HITL decisions are grouped by ``decision_at`` day; the rejection-rate
        smoothing and eval/rejection correlation are computed over the real
        series.
        """
        org_id = await _seed_org(db_engine, "trends")
        user_id = await _seed_user(db_engine, org_id, "trends")
        pipeline_id = await _seed_pipeline(db_engine, org_id, user_id, "trends")
        snapshot_id = await _seed_snapshot(db_engine, org_id, pipeline_id)
        team_id = await _seed_team(db_engine, org_id, user_id, "trends")
        run_id = await _seed_run(
            db_engine, org_id, pipeline_id, snapshot_id, status="complete", run_number=1, owner_team_id=team_id
        )

        today = datetime.now(UTC).date()
        await _seed_ledger(db_engine, org_id, today, run_count=5, spend_usd=12.5)
        await _seed_ledger(db_engine, org_id, today, run_count=2, spend_usd=3.0, team_id=team_id)

        eval_id = await _seed_eval(db_engine, org_id, pipeline_id, user_id)
        await _seed_eval_result(db_engine, org_id, run_id, eval_id, passed=True)
        await _seed_eval_result(db_engine, org_id, run_id, eval_id, passed=False)

        noon = datetime(today.year, today.month, today.day, 12, 0, tzinfo=UTC)
        await _seed_hitl_decision(
            db_engine,
            org_id,
            run_id,
            pipeline_id,
            user_id,
            review_id="dash-rev-approve",
            decision="approved",
            created_at=noon,
            decision_at=noon + timedelta(seconds=2),
        )
        await _seed_hitl_decision(
            db_engine,
            org_id,
            run_id,
            pipeline_id,
            user_id,
            review_id="dash-rev-reject",
            decision="rejected",
            created_at=noon,
            decision_at=noon + timedelta(seconds=4),
        )
        await _seed_feedback(db_engine, org_id, run_id, user_id, created_at=noon)

        payload = await _get(integration_client, org_id, user_id, "/api/v1/dashboard/trends?days=7")

        assert payload["days"] == 7, f"days echo wrong: {payload['days']}"

        assert len(payload["run_counts"]) == 1, f"only one ledger date seeded: {payload['run_counts']}"
        run_entry = payload["run_counts"][0]
        assert run_entry["date"] == today.isoformat(), f"run_counts must key the seeded date: {run_entry}"
        assert run_entry["run_count"] == 5, (
            f"trends run_counts must sum ONLY the team_id IS NULL ledger row (got {run_entry['run_count']}, expected 5)"
        )
        spend_entry = payload["token_spend"][0]
        assert spend_entry["total_spend_usd"] == 12.5, (
            "trends token_spend must sum ONLY the team_id IS NULL ledger row "
            f"(got {spend_entry['total_spend_usd']}, expected 12.5)"
        )

        assert len(payload["eval_pass_rates"]) == 1, f"only one eval date seeded: {payload['eval_pass_rates']}"
        eval_entry = payload["eval_pass_rates"][0]
        assert eval_entry["date"] == today.isoformat(), f"eval series must key the seeded date: {eval_entry}"
        assert eval_entry["total_evals"] == 2, f"both eval results must count: {eval_entry}"
        assert eval_entry["passed_evals"] == 1, f"exactly one eval passed: {eval_entry}"
        assert eval_entry["pass_rate"] == 50.0, f"pass rate wrong: {eval_entry}"

        hitl_volume = payload["hitl_volume"]
        assert len(hitl_volume) == 7, f"hitl_volume must zero-fill the window: {hitl_volume}"
        hitl_today = next(e for e in hitl_volume if e["date"] == today.isoformat())
        assert hitl_today["total_decisions"] == 2, f"both decisions must count: {hitl_today}"
        assert hitl_today["approved_count"] == 1, f"approved decision missing: {hitl_today}"
        assert hitl_today["rejected_count"] == 1, f"rejected decision missing: {hitl_today}"
        assert hitl_today["rejection_rate"] == 50.0, f"rejection rate wrong: {hitl_today}"
        assert hitl_today["avg_time_to_approve_ms"] == 3000.0, (
            "avg time to approve must average the real decision lags (2s and 4s): "
            f"{hitl_today['avg_time_to_approve_ms']}"
        )

        rejection_today = next(e for e in payload["rejection_trend"] if e["date"] == today.isoformat())
        assert rejection_today["raw_rejection_rate"] == 50.0, f"raw rejection rate wrong: {rejection_today}"

        correlation_today = next(e for e in payload["correlation"] if e["date"] == today.isoformat())
        assert correlation_today["rejection_rate"] == 50.0, f"correlation rejection leg wrong: {correlation_today}"
        assert correlation_today["eval_pass_rate"] == 50.0, f"correlation eval leg wrong: {correlation_today}"

        feedback_today = next(e for e in payload["feedback_volume"] if e["date"] == today.isoformat())
        assert feedback_today["feedback_count"] == 1, f"feedback record missing: {feedback_today}"
        assert feedback_today["resolved_count"] == 1, f"resolved feedback must count: {feedback_today}"
        assert feedback_today["correcting_count"] == 0, f"no correcting feedback seeded: {feedback_today}"

    async def test_org_sees_only_its_own_series(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        """Cross-org isolation on the trend series under real RLS.

        Org X holds a full set of series rows for today; org Y (queried)
        must see empty point series — only the zero-filled date grids.
        """
        org_x = await _seed_org(db_engine, "iso-x")
        user_x = await _seed_user(db_engine, org_x, "iso-x")
        pipeline_x = await _seed_pipeline(db_engine, org_x, user_x, "iso-x")
        snapshot_x = await _seed_snapshot(db_engine, org_x, pipeline_x)
        run_x = await _seed_run(db_engine, org_x, pipeline_x, snapshot_x, status="complete", run_number=1)
        eval_x = await _seed_eval(db_engine, org_x, pipeline_x, user_x)
        await _seed_eval_result(db_engine, org_x, run_x, eval_x, passed=True)
        await _seed_ledger(db_engine, org_x, datetime.now(UTC).date(), run_count=42, spend_usd=9.99)

        org_y = await _seed_org(db_engine, "iso-y")
        user_y = await _seed_user(db_engine, org_y, "iso-y")

        payload = await _get(integration_client, org_y, user_y, "/api/v1/dashboard/trends?days=7")

        assert not payload["run_counts"], f"org Y must not see org X's ledger rows: {payload['run_counts']}"
        assert not payload["token_spend"], f"org Y must not see org X's spend: {payload['token_spend']}"
        assert not payload["eval_pass_rates"], f"org Y must not see org X's evals: {payload['eval_pass_rates']}"
        assert {entry["total_decisions"] for entry in payload["hitl_volume"]} == {0}, (
            "org Y must see an all-zero HITL volume grid"
        )
        assert payload["feedback_volume"][-1]["feedback_count"] == 0, "org Y must see zero feedback today"


# ---------------------------------------------------------------------------
# GET /api/v1/dashboard/daily-run-counts
# ---------------------------------------------------------------------------


class TestDailyRunCounts:
    async def test_groups_recent_runs_by_day_and_status(
        self,
        integration_client: AsyncClient,
        db_engine: AsyncEngine,
    ) -> None:
        org_id = await _seed_org(db_engine, "dailycnt")
        user_id = await _seed_user(db_engine, org_id, "dailycnt")
        pipeline_id = await _seed_pipeline(db_engine, org_id, user_id, "dailycnt")
        snapshot_id = await _seed_snapshot(db_engine, org_id, pipeline_id)

        for i, status in enumerate(("complete", "complete", "failed"), start=1):
            await _seed_run(db_engine, org_id, pipeline_id, snapshot_id, status=status, run_number=i)

        payload = await _get(integration_client, org_id, user_id, "/api/v1/dashboard/daily-run-counts?days=7")

        assert payload["days"] == 7, f"days echo wrong: {payload['days']}"
        today = datetime.now(UTC).date().isoformat()
        assert today in payload["daily_counts"], f"today's bucket missing: {payload['daily_counts']}"
        today_counts = payload["daily_counts"][today]
        assert today_counts["complete"] == 2, f"complete runs grouped wrong: {today_counts}"
        assert today_counts["failed"] == 1, f"failed run grouped wrong: {today_counts}"
