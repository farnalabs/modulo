"""FAR-1274: one-PR-per-run post-run DETECTION + admin alert, outside the sandbox.

Covers three layers:

1. The pure detector (``find_duplicate_pr_urls`` / ``collect_delivery_pr_urls``)
   over the run's platform-captured delivery evidence — including the FAR-1254
   shape that motivates the ticket: a run whose SECOND ``gh pr create`` did not
   re-echo the delivery sentinel, so a sentinel-counting guard sees nothing.
   The detector reads the captured transcript (telemetry ``agent_stdout`` /
   marker ``raw_output``) plus the agent-declared ``pr_url`` fields instead,
   normalises URLs before dedup (scheme/host-case variants of one PR never
   count twice), skips ``gh pr list --json`` listing lines, and bounds how many
   URLs it collects and renders.
2. The snapshot scope gate: detection runs ONLY when the run's frozen
   pipeline snapshot declares the FAR-1273 ``single_pr_per_run`` node flag —
   multi-PR-by-design pipelines and runs with no readable snapshot are silent.
3. The terminal-write enforcement hook: a real ``update_run_status(...,
   "complete")`` against an in-memory SQLite store alerts loudly (error log +
   an admin-scoped notification written in a SAVEPOINT inside the SAME
   transaction) on a breach, stays silent on the single-PR happy path and on
   unarmed pipelines, never stacks duplicate alerts on re-terminalization, and
   never blocks the terminal status write — including when the notification
   INSERT fails at the DB level.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import StaticPool, Table, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.db.crud.run import (
    _PR_SCAN_MAX_URLS,
    NOTIFICATION_CATEGORY_DUPLICATE_PR,
    SINGLE_PR_PER_RUN_FLAG,
    _enforce_one_pr_per_run,
    _record_duplicate_pr_notification,
    _run_declares_single_pr_per_run,
    collect_delivery_pr_urls,
    find_duplicate_pr_urls,
    format_pr_url_list,
    graph_declares_single_pr_per_run,
    update_run_status,
)
from modulo.db.models.base import Base
from modulo.db.models.notification import Notification
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.run_node_outputs import RunNodeOutput
from tests.unit._store_seed import seed_run_blobs

_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
_PIPELINE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_SNAPSHOT = uuid.UUID("00000000-0000-0000-0000-0000000000b1")

_PR_1 = "https://github.com/farnalabs/modulo/pull/1001"
_PR_2 = "https://github.com/farnalabs/modulo/pull/1002"

#: What ``gh pr create`` prints for a created PR (the URL alone on a line).
#: FAR-1254: the SECOND create did NOT re-echo the pipeline's delivery
#: sentinel — only the URL — which is exactly what this transcript carries.
_TWO_PR_STDOUT = f"""Creating pull request for fix/duplicate into main in farnalabs/modulo

{_PR_1}
Creating pull request for fix/duplicate-again into main in farnalabs/modulo

{_PR_2}
"""

_ONE_PR_STDOUT = f"""Creating pull request for fix/single into main in farnalabs/modulo

{_PR_1}
"""

#: What ``gh pr list --json number,url`` prints for a PRE-EXISTING open PR the
#: delivery prompt's step-8 pre-check lists (pretty-printed JSON — the URL line
#: carries a ``"url":`` key, which the detector skips as a listing).
_PR_LISTING_SNIPPET = """[
  {
    "number": 999,
    "url": "https://github.com/farnalabs/modulo/pull/999"
  }
]"""


def _far1254_blobs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """The FAR-1254 incident shape: two PRs created, ONE sentinel echo.

    The agent declared only the first PR in its ``output.json`` ``pr_url``, the
    retention marker's single ``pr_url`` is the first raw-output match, and no
    marker carries the sentinel a second time — every single-value signal
    collapses onto ``_PR_1``. Only the captured transcript holds both URLs.
    """
    outputs = {"deliver": {"pr_url": _PR_1, "summary": "delivered"}}
    telemetry = {
        "deliver": {
            "agent_status": "completed",
            "agent_outcome": "success",
            "agent_stdout": _TWO_PR_STDOUT,
        }
    }
    markers = {
        "run:11111111-1111-1111-1111-111111111111:node:deliver:1": {
            "_modulo_marker": True,
            "status": "completed",
            "raw_output": _TWO_PR_STDOUT,
            "pr_url": _PR_1,
            "parse_error": "",
            "attempt_key": "run:11111111-1111-1111-1111-111111111111:node:deliver:1",
            "node_id": "deliver",
        }
    }
    return outputs, telemetry, markers


def _one_pr_blobs() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    outputs = {"deliver": {"pr_url": _PR_1, "summary": "delivered"}}
    telemetry = {
        "deliver": {
            "agent_status": "completed",
            "agent_outcome": "success",
            "agent_stdout": _ONE_PR_STDOUT,
        }
    }
    markers = {
        "run:11111111-1111-1111-1111-111111111111:node:deliver:1": {
            "_modulo_marker": True,
            "status": "completed",
            "raw_output": _ONE_PR_STDOUT,
            "pr_url": _PR_1,
            "parse_error": "",
            "attempt_key": "run:11111111-1111-1111-1111-111111111111:node:deliver:1",
            "node_id": "deliver",
        }
    }
    return outputs, telemetry, markers


class TestDuplicatePrDetector:
    """Pure detector over the platform-captured delivery evidence."""

    def test_far1254_second_create_without_sentinel_is_caught(self) -> None:
        """The FAR-1254 shape: two creates, ONE sentinel echo.

        Every single-value signal (declared ``pr_url``, marker ``pr_url``,
        ``delivery_done``) reports exactly one PR; only a scan of the CAPTURED
        transcript sees the second ``gh pr create`` output.
        """
        outputs, telemetry, markers = _far1254_blobs()
        urls = find_duplicate_pr_urls(outputs, telemetry, markers)
        assert urls == [_PR_1, _PR_2]

    def test_single_pr_happy_path_is_not_a_breach(self) -> None:
        outputs, telemetry, markers = _one_pr_blobs()
        assert not find_duplicate_pr_urls(outputs, telemetry, markers)

    def test_no_evidence_is_not_a_breach(self) -> None:
        assert not find_duplicate_pr_urls(None, None, None)

    def test_same_url_from_every_source_stays_one_pr(self) -> None:
        """Output + telemetry + marker all naming the SAME PR collapse to one
        distinct URL — three sightings of one PR is not a breach."""
        outputs = {"deliver": {"pr_url": _PR_1}}
        telemetry = {"deliver": {"agent_stdout": f"created {_PR_1}\n"}}
        markers = {"attempt": {"pr_url": _PR_1, "raw_output": f"created {_PR_1}\n"}}
        assert collect_delivery_pr_urls(outputs, telemetry, markers) == [_PR_1]
        assert not find_duplicate_pr_urls(outputs, telemetry, markers)

    def test_scheme_and_host_case_variants_count_once(self) -> None:
        """``http://GitHub.com/...`` vs ``https://github.com/...`` are ONE PR —
        an unnormalised dedup counts both and manufactures a false breach."""
        outputs = {"deliver": {"pr_url": "http://GitHub.com/farnalabs/modulo/pull/1001"}}
        telemetry = {"deliver": {"agent_stdout": f"{_PR_1}\n"}}
        urls = collect_delivery_pr_urls(outputs, telemetry, None)
        assert len(urls) == 1
        assert not find_duplicate_pr_urls(outputs, telemetry, None)

    def test_pr_list_json_listing_lines_do_not_count(self) -> None:
        """The delivery prompt's step-8 pre-check runs ``gh pr list --json
        ...,url``, which prints OTHER open PRs' URLs as JSON — a listing of
        pre-existing PRs must not read as a second PR created by this run."""
        outputs = {"deliver": {"pr_url": _PR_1}}
        telemetry = {"deliver": {"agent_stdout": f"{_ONE_PR_STDOUT}{_PR_LISTING_SNIPPET}\n"}}
        urls = collect_delivery_pr_urls(outputs, telemetry, None)
        assert urls == [_PR_1]
        assert not find_duplicate_pr_urls(outputs, telemetry, None)

    def test_transcript_only_second_url_still_breaches(self) -> None:
        """A second URL that appears ONLY in captured stdout (the agent never
        declared it) is still a breach — detection needs no agent cooperation."""
        outputs = {"deliver": {"pr_url": _PR_1}}
        telemetry = {"deliver": {"agent_stdout": f"{_PR_1}\n{_PR_2}\n"}}
        markers: dict[str, Any] | None = None
        urls = find_duplicate_pr_urls(outputs, telemetry, markers)
        assert urls == [_PR_1, _PR_2]

    def test_declared_only_second_url_still_breaches(self) -> None:
        """The transcript may be truncated away; two DECLARED urls still breach."""
        outputs = {"deliver": {"pr_url": _PR_1}, "review": {"pr_url": _PR_2}}
        urls = find_duplicate_pr_urls(outputs, None, None)
        assert urls == [_PR_1, _PR_2]

    def test_non_pr_github_urls_do_not_count(self) -> None:
        """A repo/issue link is not a pull request URL."""
        outputs = {
            "deliver": {
                "pr_url": _PR_1,
                "summary": "see https://github.com/farnalabs/modulo/issues/7 and the repo home",
            }
        }
        assert not find_duplicate_pr_urls(outputs, None, None)

    def test_collection_deduplicates_preserving_first_seen_order(self) -> None:
        outputs = {"a": {"pr_url": _PR_2}}
        telemetry = {"deliver": {"agent_stdout": f"{_PR_1}\n{_PR_2}\n{_PR_1}\n"}}
        urls = collect_delivery_pr_urls(outputs, telemetry, None)
        assert urls == [_PR_2, _PR_1]

    def test_url_collection_is_bounded(self) -> None:
        """The walk stops at the collector ceiling — a pathological
        evidence-flood blob cannot grow the list without bound."""
        transcript = "\n".join(f"https://github.com/farnalabs/modulo/pull/{n}" for n in range(1, 201))
        urls = collect_delivery_pr_urls({"deliver": {"agent_stdout": transcript}}, None, None)
        assert len(urls) == _PR_SCAN_MAX_URLS

    def test_format_pr_url_list_truncates_with_more_suffix(self) -> None:
        """Only the first ten URLs are rendered; the rest become "(and N more)"
        so the alert body/log line stays bounded."""
        pr_urls = [f"https://github.com/farnalabs/modulo/pull/{n}" for n in range(1, 13)]
        rendered = format_pr_url_list(pr_urls)
        assert rendered.endswith("(and 2 more)")
        assert rendered.count("https://github.com") == 10
        assert "/pull/11" not in rendered

    def test_cyclic_blob_terminates(self) -> None:
        """A self-referential blob must not recurse forever."""
        cyclic: dict[str, Any] = {"pr_url": _PR_1}
        cyclic["self"] = cyclic
        telemetry = {"deliver": {"agent_stdout": f"{_PR_2}\n", "cycle": cyclic}}
        urls = collect_delivery_pr_urls(None, telemetry, None)
        assert urls == [_PR_2, _PR_1]

    def test_depth_bound_terminates_on_pathological_nesting(self) -> None:
        """Nesting past the scan depth is ignored rather than walked unbounded."""
        deep: dict[str, Any] = {"pr_url": _PR_2}
        for _ in range(12):
            deep = {"nested": deep}
        urls = collect_delivery_pr_urls(deep, None, None)
        assert not urls

    def test_list_and_tuple_containers_are_walked(self) -> None:
        """Node returns arrive as list envelopes, not only dicts: a PR URL inside
        a list or tuple is collected just like one in a mapping."""
        outputs = {"deliver": [{"pr_url": _PR_1}]}
        telemetry = {"deliver": ({"agent_stdout": f"{_PR_2}\n"},)}
        urls = collect_delivery_pr_urls(outputs, telemetry, None)
        assert urls == [_PR_1, _PR_2]

    def test_self_referential_list_terminates(self) -> None:
        """A cyclic LIST (not just a dict) terminates via the seen-id set rather
        than recursing forever."""
        cyclic: list[Any] = [f"{_PR_1}\n"]
        cyclic.append(cyclic)
        urls = collect_delivery_pr_urls(cyclic, None, None)
        assert urls == [_PR_1]


class TestSnapshotScopeGate:
    """The FAR-1273 flag gate: only runs whose snapshot declares the contract."""

    def test_flag_true_on_a_node_declares_the_contract(self) -> None:
        graph = {"nodes": [{"id": "deliver", "node_type": "sandbox_agent", SINGLE_PR_PER_RUN_FLAG: True}]}
        assert graph_declares_single_pr_per_run(graph)

    def test_flag_nested_under_a_policy_object_is_found(self) -> None:
        graph = {"nodes": [{"id": "deliver", "sandbox_policy": {SINGLE_PR_PER_RUN_FLAG: True}}]}
        assert graph_declares_single_pr_per_run(graph)

    def test_absent_flag_does_not_declare_the_contract(self) -> None:
        """Multi-PR-by-design pipelines never set the flag — they must be
        silent even when their evidence holds many PR URLs."""
        graph = {"nodes": [{"id": "deliver", "node_type": "sandbox_agent"}]}
        assert not graph_declares_single_pr_per_run(graph)

    def test_unreadable_graph_fails_safe_to_silent(self) -> None:
        """No snapshot row / no graph / no nodes -> NOT armed (a read failure
        must never manufacture a false alert)."""
        assert not graph_declares_single_pr_per_run(None)
        assert not graph_declares_single_pr_per_run("not-a-dict")
        assert not graph_declares_single_pr_per_run({"no_nodes": []})

    def test_flag_deeper_than_the_scan_bound_is_not_found(self) -> None:
        """A flag nested deeper than the bounded scan is never found — the walk
        stops at *_SINGLE_PR_FLAG_SCAN_DEPTH* instead of recursing without
        limit, so a pathological graph cannot hang detection."""
        deep: Any = {SINGLE_PR_PER_RUN_FLAG: True}
        for _ in range(8):
            deep = {"nested": deep}
        assert not graph_declares_single_pr_per_run({"nodes": [deep]})

    def test_flag_inside_a_list_value_on_a_node_is_found(self) -> None:
        """The walk descends list-valued node fields too, not only dicts, so a
        contract declared inside a step/config list still arms detection."""
        graph = {"nodes": [{"id": "deliver", "steps": [{"config": {SINGLE_PR_PER_RUN_FLAG: True}}]}]}
        assert graph_declares_single_pr_per_run(graph)

    async def test_run_without_a_frozen_snapshot_is_not_armed(self) -> None:
        """A run carrying no ``snapshot_id`` (detached / pre-snapshot) is not
        armed: the gate returns before touching the session."""
        run = SimpleNamespace(snapshot_id=None)
        session = cast(AsyncSession, SimpleNamespace())
        assert await _run_declares_single_pr_per_run(session, cast(Any, run)) is False


# ---------------------------------------------------------------------------
# Terminal-write enforcement — in-memory SQLite (real Run/Notification tables)
# ---------------------------------------------------------------------------


_TABLES: list[Table] = cast(
    list[Table],
    [
        Run.__table__,
        # The terminalization hook's blob reads go through the run_node_outputs
        # repo reader — the table must exist on this engine.
        RunNodeOutput.__table__,
        # FAR-1274: the breach alert is written in the terminal transaction.
        Notification.__table__,
        # The scope gate reads the run's FROZEN snapshot graph.
        PipelineSnapshot.__table__,
    ],
)


@pytest.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    # StaticPool: an in-memory SQLite DB is per-connection; the pool shares ONE
    # connection so sessions AND independent re-read connections all observe the
    # same database.
    eng = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES))
        await conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    # autobegin=False matches the production DI factory: every DB operation must
    # sit inside an explicit ``async with session.begin():`` block.
    maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    async with maker() as s:
        yield s


async def _seed_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    outputs: dict[str, Any] | None = None,
    telemetry: dict[str, Any] | None = None,
    markers: dict[str, Any] | None = None,
    single_pr_per_run: bool = True,
    seed_snapshot: bool = True,
) -> Run:
    """Seed a running run (+ optional evidence blobs + its frozen snapshot).

    ``single_pr_per_run=False`` seeds a snapshot whose node does NOT declare
    the FAR-1273 contract (multi-PR-by-design pipeline); ``seed_snapshot=False``
    points the run at a snapshot row that does not exist (unreadable-snapshot
    fail-safe).
    """
    if seed_snapshot:
        graph_nodes: list[dict[str, Any]] = [{"id": "deliver", "node_type": "sandbox_agent"}]
        if single_pr_per_run:
            graph_nodes[0][SINGLE_PR_PER_RUN_FLAG] = True
        session.add(
            PipelineSnapshot(
                id=_SNAPSHOT,
                organisation_id=_ORG,
                pipeline_id=_PIPELINE,
                snapshot_version=1,
                graph_json={"nodes": graph_nodes, "edges": []},
                connector_bindings_json=[],
                schema_pins_json=[],
                prompt_pins_json=[],
                model_backend_pins_json=[],
            )
        )
        await session.flush()
    run = Run(
        id=run_id,
        organisation_id=_ORG,
        pipeline_id=_PIPELINE,
        snapshot_id=_SNAPSHOT if seed_snapshot else uuid.uuid4(),
        trigger_type="manual",
        status="running",
        run_number=int(run_id.int % 10**9) + 1,
        input_hash="ih",
        input_payload={},
        langgraph_thread_id=f"thread-{run_id}",
        claim_token="tok-a",
        cancellation_requested=False,
    )
    session.add(run)
    await session.flush()
    await seed_run_blobs(
        session,
        run.id,
        outputs=outputs,
        telemetry=telemetry,
        markers=markers,
        organisation_id=_ORG,
    )
    return run


async def _notifications_for(engine: AsyncEngine, run_id: uuid.UUID) -> list[Notification]:
    maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    async with maker() as s, s.begin():
        rows = await s.execute(select(Notification).where(Notification.action_url == f"/runs/{run_id}"))
        return list(rows.scalars().all())


async def _stored_run(engine: AsyncEngine, run_id: uuid.UUID) -> Run:
    maker = async_sessionmaker(engine, expire_on_commit=False, autobegin=False)
    async with maker() as s, s.begin():
        return (await s.execute(select(Run).where(Run.id == run_id))).scalar_one()


class TestTerminalWriteEnforcement:
    async def test_breach_alerts_loudly_on_terminal_complete(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A FAR-1254-shaped run terminalizes ``complete`` -> ERROR log + one
        admin-scoped, error-level notification naming BOTH PR URLs."""
        run_id = uuid.uuid4()
        outputs, telemetry, markers = _far1254_blobs()
        with caplog.at_level(logging.ERROR, logger="modulo.db.crud.run"):
            async with session.begin():
                await _seed_run(session, run_id, outputs=outputs, telemetry=telemetry, markers=markers)
                updated = await update_run_status(session, run_id, "complete")

        assert updated is not None
        assert updated.status == "complete"
        assert "delivery_contract.duplicate_pr" in caplog.text

        rows = await _notifications_for(engine, run_id)
        assert len(rows) == 1
        alert = rows[0]
        assert alert.category == NOTIFICATION_CATEGORY_DUPLICATE_PR
        assert alert.scope == "admin"
        assert alert.level == "error"
        assert alert.action_url == f"/runs/{run_id}"
        # The alert belongs to the RUN's org (never cross-tenant).
        assert alert.organisation_id == _ORG
        assert _PR_1 in alert.body
        assert _PR_2 in alert.body

    async def test_production_shape_blobs_written_by_the_terminal_write_alert(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """PRODUCTION WRITE SHAPE: nothing pre-seeded — the terminal write
        ITSELF carries ``outputs_json`` / ``node_telemetry_json``, the blobs
        land in this transaction, and the hook (which runs AFTER the blob
        write) must see them and alert."""
        run_id = uuid.uuid4()
        outputs, telemetry, _markers = _far1254_blobs()
        async with session.begin():
            await _seed_run(session, run_id)
            updated = await update_run_status(
                session,
                run_id,
                "complete",
                outputs_json=outputs,
                node_telemetry_json=telemetry,
            )

        assert updated is not None
        assert updated.status == "complete"
        rows = await _notifications_for(engine, run_id)
        assert len(rows) == 1
        alert = rows[0]
        assert alert.category == NOTIFICATION_CATEGORY_DUPLICATE_PR
        assert alert.organisation_id == _ORG
        assert _PR_1 in alert.body
        assert _PR_2 in alert.body

    async def test_single_pr_happy_path_stays_silent(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """Control: the normal one-PR delivery terminalizes with NO alert and
        still classifies as a delivered run."""
        run_id = uuid.uuid4()
        outputs, telemetry, markers = _one_pr_blobs()
        async with session.begin():
            await _seed_run(session, run_id, outputs=outputs, telemetry=telemetry, markers=markers)
            updated = await update_run_status(session, run_id, "complete")

        assert updated is not None
        assert updated.status == "complete"
        rows = await _notifications_for(engine, run_id)
        assert not rows

        stored = await _stored_run(engine, run_id)
        classification = stored.run_classification
        assert isinstance(classification, dict)
        assert classification["value"] == "delivered"

    async def test_multi_pr_by_design_pipeline_without_flag_is_silent(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A pipeline that legitimately delivers MANY PRs (batch ticket-to-PR)
        never declares ``single_pr_per_run`` — its evidence full of PR URLs must
        produce NO alert and NO error log, while the run still terminalizes."""
        run_id = uuid.uuid4()
        outputs, telemetry, markers = _far1254_blobs()
        with caplog.at_level(logging.ERROR, logger="modulo.db.crud.run"):
            async with session.begin():
                await _seed_run(
                    session,
                    run_id,
                    outputs=outputs,
                    telemetry=telemetry,
                    markers=markers,
                    single_pr_per_run=False,
                )
                updated = await update_run_status(session, run_id, "complete")

        assert updated is not None
        assert updated.status == "complete"
        rows = await _notifications_for(engine, run_id)
        assert not rows
        assert "delivery_contract.duplicate_pr" not in caplog.text

        stored = await _stored_run(engine, run_id)
        classification = stored.run_classification
        assert isinstance(classification, dict)
        assert classification["value"] == "delivered"

    async def test_unreadable_snapshot_fails_safe_to_silent(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """No snapshot row -> the contract cannot be read -> NO alert (a read
        failure is a missed detection, never a false alert)."""
        run_id = uuid.uuid4()
        outputs, telemetry, markers = _far1254_blobs()
        async with session.begin():
            await _seed_run(
                session,
                run_id,
                outputs=outputs,
                telemetry=telemetry,
                markers=markers,
                seed_snapshot=False,
            )
            updated = await update_run_status(session, run_id, "complete")

        assert updated is not None
        assert updated.status == "complete"
        rows = await _notifications_for(engine, run_id)
        assert not rows

    async def test_pr_list_precheck_noise_does_not_alert(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """Armed pipeline, ONE created PR, and a step-8 ``gh pr list --json``
        pre-check listing other open PRs in stdout -> still NO alert."""
        run_id = uuid.uuid4()
        outputs = {"deliver": {"pr_url": _PR_1, "summary": "delivered"}}
        telemetry = {"deliver": {"agent_stdout": f"{_ONE_PR_STDOUT}{_PR_LISTING_SNIPPET}\n"}}
        async with session.begin():
            await _seed_run(session, run_id, outputs=outputs, telemetry=telemetry)
            updated = await update_run_status(session, run_id, "complete")

        assert updated is not None
        assert updated.status == "complete"
        rows = await _notifications_for(engine, run_id)
        assert not rows

    async def test_missing_org_id_degrades_to_a_logged_error(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A run with no ``organisation_id`` cannot address an admin-scoped
        alert: the breach is logged and the helper returns without touching the
        session."""
        run = SimpleNamespace(id=uuid.uuid4(), run_number=7)
        with caplog.at_level(logging.ERROR, logger="modulo.db.crud.run"):
            await _record_duplicate_pr_notification(cast(AsyncSession, None), cast(Any, run), [_PR_1, _PR_2])

        assert "duplicate_pr_missing_org" in caplog.text

    async def test_cancellation_is_propagated_never_swallowed(
        self,
        session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cancelled blob read must propagate: the best-effort hook re-raises
        ``CancelledError`` instead of logging it as a miss — and the SAVEPOINT
        rollback around the alert path must not swallow it either."""
        from unittest.mock import AsyncMock

        run = SimpleNamespace(id=uuid.uuid4(), organisation_id=_ORG)
        monkeypatch.setattr(
            "modulo.db.crud.run._run_declares_single_pr_per_run",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            "modulo.db.crud.run.read_run_node_outputs_raw",
            AsyncMock(side_effect=asyncio.CancelledError()),
        )
        with pytest.raises(asyncio.CancelledError):
            async with session.begin():
                await _enforce_one_pr_per_run(session, cast(Any, run))

    async def test_alert_body_truncates_long_url_lists(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """The rendered body is bounded: twelve distinct PR URLs render ten
        plus an "(and 2 more)" suffix — never the full list."""
        run_id = uuid.uuid4()
        transcript = "\n".join(f"https://github.com/farnalabs/modulo/pull/{n}" for n in range(1, 13))
        telemetry = {"deliver": {"agent_stdout": transcript + "\n"}}
        async with session.begin():
            await _seed_run(session, run_id, telemetry=telemetry)
            updated = await update_run_status(session, run_id, "complete")

        assert updated is not None
        rows = await _notifications_for(engine, run_id)
        assert len(rows) == 1
        body = rows[0].body
        assert "(and 2 more)" in body
        assert body.count("https://github.com") == 10

    async def test_alert_is_atomic_with_the_terminal_write(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """The alert rides the terminalization TRANSACTION: rolling that
        transaction back removes the alert, so a phantom breach can never
        survive a terminal write that did not commit."""

        async def _terminalize_then_abort(run_id: uuid.UUID, blobs: tuple[Any, Any, Any]) -> None:
            outputs, telemetry, markers = blobs
            async with session.begin():
                await _seed_run(session, run_id, outputs=outputs, telemetry=telemetry, markers=markers)
                await update_run_status(session, run_id, "complete")
                raise RuntimeError("terminalization rolled back")

        run_id = uuid.uuid4()
        with pytest.raises(RuntimeError, match="terminalization rolled back"):
            await _terminalize_then_abort(run_id, _far1254_blobs())

        rows = await _notifications_for(engine, run_id)
        assert not rows

    async def test_db_level_alert_failure_still_commits_terminal_status(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """SAVEPOINT proof: a DB-level failure of the notification INSERT must
        roll back ONLY the alert, never the caller's terminal status.

        A trigger makes the INSERT itself fail (a real DB error, not a mocked
        exception). Without the savepoint that error would poison the enclosing
        transaction on Postgres and the caller's commit would silently roll
        back the already-flushed ``complete`` status — a stuck run. Here the
        run row still commits terminal, the alert row does not exist, and the
        failure is logged."""
        run_id = uuid.uuid4()
        outputs, telemetry, markers = _far1254_blobs()
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "CREATE TRIGGER trg_block_dup_pr_alert "
                "BEFORE INSERT ON notifications "
                "WHEN NEW.category = 'run.duplicate_pr_delivery' "
                "BEGIN SELECT RAISE(ABORT, 'simulated db-level alert failure'); END"
            )
        with caplog.at_level(logging.ERROR, logger="modulo.db.crud.run"):
            async with session.begin():
                await _seed_run(session, run_id, outputs=outputs, telemetry=telemetry, markers=markers)
                updated = await update_run_status(session, run_id, "complete")

        assert updated is not None
        assert updated.status == "complete"
        stored = await _stored_run(engine, run_id)
        assert stored.status == "complete"
        rows = await _notifications_for(engine, run_id)
        assert not rows
        assert "delivery_contract.enforcement_failed" in caplog.text

    async def test_reterminalization_does_not_stack_alerts(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """A retry policy re-terminalizing the run must not stack a second
        identical alert for the same run."""
        run_id = uuid.uuid4()
        outputs, telemetry, markers = _far1254_blobs()
        async with session.begin():
            await _seed_run(session, run_id, outputs=outputs, telemetry=telemetry, markers=markers)
            await update_run_status(session, run_id, "complete")
        async with session.begin():
            await update_run_status(session, run_id, "complete")

        rows = await _notifications_for(engine, run_id)
        assert len(rows) == 1

    async def test_failed_run_with_two_prs_still_alerts(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
    ) -> None:
        """Detection covers every terminal writer that funnels through the hook,
        not only ``complete``: a run that created two PRs then FAILED is a
        breach too."""
        run_id = uuid.uuid4()
        outputs, telemetry, markers = _far1254_blobs()
        async with session.begin():
            await _seed_run(session, run_id, outputs=outputs, telemetry=telemetry, markers=markers)
            updated = await update_run_status(session, run_id, "failed", error_code="node.cancelled")

        assert updated is not None
        assert updated.status == "failed"
        rows = await _notifications_for(engine, run_id)
        assert len(rows) == 1

    async def test_evidence_read_failure_never_blocks_terminalization(
        self,
        engine: AsyncEngine,
        session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Detection is best-effort: a failing blob read degrades to a logged
        miss and the terminal status write still lands."""
        from unittest.mock import AsyncMock

        run_id = uuid.uuid4()
        outputs, telemetry, markers = _far1254_blobs()
        monkeypatch.setattr(
            "modulo.db.crud.run.read_run_node_outputs_raw",
            AsyncMock(side_effect=RuntimeError("store unreachable")),
        )
        async with session.begin():
            await _seed_run(session, run_id, outputs=outputs, telemetry=telemetry, markers=markers)
            updated = await update_run_status(session, run_id, "complete")

        assert updated is not None
        assert updated.status == "complete"
        rows = await _notifications_for(engine, run_id)
        assert not rows
