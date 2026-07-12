"""BDD step definitions: Agent Signal Triggers.

Covers cross-pipeline signal triggers (PRD §8.5).
"""

import asyncio
import contextlib
import uuid
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_bdd import given, parsers, scenarios, then, when

from modulo.core.trigger_engine.agent_signal import fire_agent_signal
from tests.bdd.conftest import ALT_ORG_ID, ORG_ID

with contextlib.suppress(FileNotFoundError, OSError):
    scenarios("../features/triggers/agent_signal.feature")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORG_MAP: dict[str, uuid.UUID] = {
    "acme": ORG_ID,
    "other-corp": ALT_ORG_ID,
}


def _org_id(name: str) -> uuid.UUID:
    return _ORG_MAP.get(name, ORG_ID)


def _ctx(request: Any) -> dict[str, Any]:
    if not hasattr(request.node, "_ctx"):
        request.node._ctx = {
            "triggers": [],
            "org_id": ORG_ID,
            "source_pipeline_id": uuid.uuid4(),
            "completed_node_id": None,
            "source_run_id": uuid.uuid4(),
            "node_output": None,
            "active_runs_count": 0,
            "results": None,
            "mock_create_run": None,
            "created_child_run": None,
        }
    return cast("dict[str, Any]", request.node._ctx)


def _make_trigger(
    *,
    trigger_id: uuid.UUID | None = None,
    pipeline_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    source_pipeline_id: uuid.UUID | None = None,
    source_node_id: str = "extract",
    active: bool = True,
    max_concurrent_runs: int = 5,
    snapshot_id: str | None = None,
) -> MagicMock:
    tid = trigger_id or uuid.uuid4()
    pid = pipeline_id or uuid.uuid4()
    oid = org_id or ORG_ID
    spid = source_pipeline_id or uuid.uuid4()

    trigger = MagicMock()
    trigger.id = tid
    trigger.pipeline_id = pid
    trigger.organisation_id = oid
    trigger.active = active
    trigger.max_concurrent_runs = max_concurrent_runs
    trigger.config_json = {
        "source_pipeline_id": str(spid),
        "source_node_id": source_node_id,
        "snapshot_id": snapshot_id,
    }
    return trigger


def _setup_session(session: MagicMock, triggers: list[Any], count: int = 0) -> None:
    trigger_result = MagicMock()
    trigger_result.scalars.return_value.all.return_value = triggers
    count_result = MagicMock()
    count_result.scalar_one.return_value = count
    call_num: list[int] = [0]

    async def side_effect(*args: Any, **kwargs: Any) -> Any:
        call_num[0] += 1
        if call_num[0] == 1:
            return trigger_result
        return count_result

    session.execute = side_effect


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(parsers.parse('a source pipeline "{name}" exists'))
def _given_source_pipeline(name: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["source_pipeline_id"] = uuid.uuid4()


@given(
    parsers.parse('pipeline "{name}" has an agent_signal trigger watching source pipeline "{source}" node "{node_id}"')
)
def _given_trigger_watching(name: str, source: str, node_id: str, request: Any) -> None:
    ctx = _ctx(request)
    spid = ctx["source_pipeline_id"]
    trigger = _make_trigger(
        pipeline_id=uuid.uuid4(),
        source_pipeline_id=spid,
        source_node_id=node_id,
    )
    ctx["triggers"].append(trigger)
    ctx["completed_node_id"] = node_id


@given(parsers.parse('no agent_signal trigger watches source pipeline "{source}" node "{node_id}"'))
def _given_no_trigger(source: str, node_id: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["triggers"] = []
    ctx["completed_node_id"] = node_id


@given(parsers.parse('pipeline "{name}" has an agent_signal trigger with max_concurrent_runs {limit:d}'))
def _given_trigger_with_limit(name: str, limit: int, request: Any) -> None:
    ctx = _ctx(request)
    spid = ctx["source_pipeline_id"]
    trigger = _make_trigger(
        pipeline_id=uuid.uuid4(),
        source_pipeline_id=spid,
        source_node_id="extract",
        max_concurrent_runs=limit,
    )
    ctx["triggers"].append(trigger)
    ctx["completed_node_id"] = "extract"


@given(parsers.parse('pipeline "{name}" has {count:d} active run'))
def _given_active_runs(name: str, count: int, request: Any) -> None:
    ctx = _ctx(request)
    ctx["active_runs_count"] = count


@given(parsers.parse('pipeline "{name}" has an active run'))
def _given_one_active_run(name: str, request: Any) -> None:
    ctx = _ctx(request)
    ctx["active_runs_count"] = 1


@given(
    parsers.parse(
        'pipeline "{name}" has an inactive agent_signal trigger watching source pipeline "{source}" node "{node_id}"'
    )
)
def _given_inactive_trigger(name: str, source: str, node_id: str, request: Any) -> None:
    ctx = _ctx(request)
    spid = ctx["source_pipeline_id"]
    trigger = _make_trigger(
        pipeline_id=uuid.uuid4(),
        source_pipeline_id=spid,
        source_node_id=node_id,
        active=False,
    )
    ctx["triggers"].append(trigger)
    ctx["completed_node_id"] = node_id


@given(parsers.parse('org "{org}" has an agent_signal trigger watching source pipeline "{source}" node "{node_id}"'))
def _given_other_org_trigger(org: str, source: str, node_id: str, request: Any) -> None:
    ctx = _ctx(request)
    oid = _org_id(org)
    spid = ctx["source_pipeline_id"]
    trigger = _make_trigger(
        org_id=oid,
        pipeline_id=uuid.uuid4(),
        source_pipeline_id=spid,
        source_node_id=node_id,
    )
    ctx["triggers"].append(trigger)
    ctx["completed_node_id"] = node_id


@given(parsers.parse('pipeline "{name}" has an agent_signal trigger with snapshot_id "{snapshot_id}"'))
def _given_trigger_bad_snapshot(name: str, snapshot_id: str, request: Any) -> None:
    ctx = _ctx(request)
    spid = ctx["source_pipeline_id"]
    trigger = _make_trigger(
        pipeline_id=uuid.uuid4(),
        source_pipeline_id=spid,
        source_node_id="extract",
        snapshot_id=snapshot_id,
    )
    ctx["triggers"].append(trigger)
    ctx["completed_node_id"] = "extract"


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when(parsers.parse('node "{node_id}" completes in pipeline "{source}"'))
def _when_node_completes(node_id: str, source: str, mock_session: MagicMock, request: Any) -> None:
    ctx = _ctx(request)
    ctx["completed_node_id"] = node_id
    # Simulate SQL WHERE active=True AND organisation_id=<org_id>
    filtered = [t for t in ctx["triggers"] if t.active and str(t.organisation_id) == str(ctx["org_id"])]
    _setup_session(mock_session, filtered, ctx.get("active_runs_count", 0))

    with patch("modulo.core.trigger_engine.agent_signal.create_run", new_callable=AsyncMock) as mock_cr:
        child_run = MagicMock(id=uuid.uuid4())
        mock_cr.return_value = child_run
        ctx["mock_create_run"] = mock_cr
        ctx["created_child_run"] = child_run

        ctx["results"] = asyncio.run(
            fire_agent_signal(
                mock_session,
                org_id=ctx["org_id"],
                source_run_id=ctx["source_run_id"],
                source_pipeline_id=ctx["source_pipeline_id"],
                completed_node_id=node_id,
                node_output=ctx.get("node_output"),
            )
        )


@when(parsers.parse('node "{node_id}" completes in pipeline "{source}" with output {output}'))
def _when_node_completes_with_output(
    node_id: str, source: str, output: str, mock_session: MagicMock, request: Any
) -> None:
    ctx = _ctx(request)
    ctx["completed_node_id"] = node_id
    ctx["node_output"] = output if isinstance(output, dict) else {"result": output}
    # Simulate SQL WHERE active=True AND organisation_id=<org_id>
    filtered = [t for t in ctx["triggers"] if t.active and str(t.organisation_id) == str(ctx["org_id"])]
    _setup_session(mock_session, filtered, ctx.get("active_runs_count", 0))

    with patch("modulo.core.trigger_engine.agent_signal.create_run", new_callable=AsyncMock) as mock_cr:
        child_run = MagicMock(id=uuid.uuid4())
        mock_cr.return_value = child_run
        ctx["mock_create_run"] = mock_cr
        ctx["created_child_run"] = child_run

        ctx["results"] = asyncio.run(
            fire_agent_signal(
                mock_session,
                org_id=ctx["org_id"],
                source_run_id=ctx["source_run_id"],
                source_pipeline_id=ctx["source_pipeline_id"],
                completed_node_id=node_id,
                node_output=ctx["node_output"],
            )
        )


@when(parsers.parse('node "{node_id}" completes with output {output}'))
def _when_node_completes_with_output_simple(node_id: str, output: str, mock_session: MagicMock, request: Any) -> None:
    ctx = _ctx(request)
    ctx["completed_node_id"] = node_id
    ctx["node_output"] = output if isinstance(output, dict) else {"result": output}
    # Simulate SQL WHERE active=True AND organisation_id=<org_id>
    filtered = [t for t in ctx["triggers"] if t.active and str(t.organisation_id) == str(ctx["org_id"])]
    _setup_session(mock_session, filtered, ctx.get("active_runs_count", 0))

    with patch("modulo.core.trigger_engine.agent_signal.create_run", new_callable=AsyncMock) as mock_cr:
        child_run = MagicMock(id=uuid.uuid4())
        mock_cr.return_value = child_run
        ctx["mock_create_run"] = mock_cr
        ctx["created_child_run"] = child_run

        ctx["results"] = asyncio.run(
            fire_agent_signal(
                mock_session,
                org_id=ctx["org_id"],
                source_run_id=ctx["source_run_id"],
                source_pipeline_id=ctx["source_pipeline_id"],
                completed_node_id=node_id,
                node_output=ctx["node_output"],
            )
        )


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then(parsers.parse('a child run is created for pipeline "{name}"'))
def _then_child_run_created(name: str, request: Any) -> None:
    ctx = _ctx(request)
    mock_cr = ctx.get("mock_create_run")
    assert mock_cr is not None
    mock_cr.assert_awaited_once()
    call_kwargs = mock_cr.call_args[1]
    assert call_kwargs["trigger_type"] == "agent_signal"


@then(parsers.parse('the child run has trigger_type "{ttype}"'))
def _then_child_run_trigger_type(ttype: str, request: Any) -> None:
    ctx = _ctx(request)
    mock_cr = ctx.get("mock_create_run")
    assert mock_cr is not None
    call_kwargs = mock_cr.call_args[1]
    assert call_kwargs["trigger_type"] == ttype


@then(parsers.parse('a TriggerEvent is recorded with result "{result}"'))
def _then_trigger_event_recorded(result: str, request: Any) -> None:
    session = request.getfixturevalue("mock_session")
    assert session.add.call_count >= 1
    added_events = [c.args[0] for c in session.add.call_args_list if hasattr(c.args[0], "validation_result")]
    assert any(e.validation_result == result for e in added_events), (
        f"No TriggerEvent with validation_result='{result}' found"
    )


@then("the result is empty")
def _then_result_empty(request: Any) -> None:
    ctx = _ctx(request)
    assert ctx["results"] == []


@then("no child run is created")
def _then_no_child_run(request: Any) -> None:
    ctx = _ctx(request)
    mock_cr = ctx.get("mock_create_run")
    if mock_cr is not None:
        mock_cr.assert_not_called()


@then(parsers.parse('the signal is skipped with reason "{reason}"'))
def _then_signal_skipped(reason: str, request: Any) -> None:
    ctx = _ctx(request)
    results = ctx["results"]
    assert len(results) == 1
    assert results[0]["status"] == "skipped"
    assert results[0]["reason"] == reason


@then(parsers.parse("{count:d} child runs are created"))
def _then_n_child_runs(count: int, request: Any) -> None:
    ctx = _ctx(request)
    mock_cr = ctx.get("mock_create_run")
    assert mock_cr is not None
    assert mock_cr.await_count == count


@then(parsers.parse('both results have status "{status}"'))
def _then_both_status(status: str, request: Any) -> None:
    ctx = _ctx(request)
    results = ctx["results"]
    assert all(r["status"] == status for r in results)


@then(parsers.parse('no child run is created in org "{org}"'))
def _then_no_child_run_in_org(org: str, request: Any) -> None:
    ctx = _ctx(request)
    mock_cr = ctx.get("mock_create_run")
    if mock_cr is not None:
        mock_cr.assert_not_called()


@then(parsers.parse('the child run input_payload contains "{key}"'))
def _then_input_payload_contains(key: str, request: Any) -> None:
    ctx = _ctx(request)
    mock_cr = ctx.get("mock_create_run")
    assert mock_cr is not None
    call_kwargs = mock_cr.call_args[1]
    input_payload = call_kwargs["input_payload"]
    assert key in input_payload, f"input_payload does not contain '{key}': {input_payload}"


@then("a child run is created with a valid UUID snapshot_id")
def _then_valid_snapshot_id(request: Any) -> None:
    ctx = _ctx(request)
    mock_cr = ctx.get("mock_create_run")
    assert mock_cr is not None
    mock_cr.assert_awaited_once()
    call_kwargs = mock_cr.call_args[1]
    snapshot_id = call_kwargs["snapshot_id"]
    assert isinstance(snapshot_id, uuid.UUID), f"snapshot_id is not a UUID: {snapshot_id}"


@then("a TriggerEvent is recorded for the fire attempt")
def _then_trigger_event_any(request: Any) -> None:
    session = request.getfixturevalue("mock_session")
    assert session.add.call_count >= 1
    added_events = [c.args[0] for c in session.add.call_args_list if hasattr(c.args[0], "validation_result")]
    assert len(added_events) >= 1, "No TriggerEvent was added to the session"
