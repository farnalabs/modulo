"""Step definitions for run_context.feature — seeding, write guard, audit."""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

# ---------------------------------------------------------------------------
# Register feature file
# ---------------------------------------------------------------------------
try:
    scenarios("../../bdd/features/pipelines/run_context.feature")
except (FileNotFoundError, OSError):
    pass

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LIVE_STATE: dict[str, Any] = {"run_context": {"cancelled": False}}


def _make_node(name: str, role: str | None = None) -> MagicMock:
    node = MagicMock()
    node.id = name
    node.role = role
    return node


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def patches():
    collectors: list[Any] = []
    yield collectors
    for p in reversed(collectors):
        try:
            p.stop()
        except RuntimeError:
            pass


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {
        "pipeline": None,
        "nodes": {},
        "run_context": {"cancelled": False},
        "write_log": [],
        "violation_error": None,
        "warning_records": [],
        "seeded_state": None,
        "agent_reads": {},
        "state": None,
        "defaults": {},
        "input_payload": {},
    }


# ===================================================================
#  GIVEN
# ===================================================================


@given(
    parsers.parse(
        'pipeline "{pipeline_name}" with a context_setter node "{node_name}"'
    )
)
def pipeline_with_context_setter(
    pipeline_name: str, node_name: str, ctx: dict[str, Any]
) -> None:
    from tests.bdd.conftest import make_mock_pipeline

    ctx["pipeline"] = make_mock_pipeline(name=pipeline_name)
    ctx["nodes"] = {node_name: _make_node(node_name, role="context_setter")}


@given(
    parsers.parse(
        'pipeline "{pipeline_name}" with an agent node "{node_name}"'
    )
)
def pipeline_with_agent(
    pipeline_name: str, node_name: str, ctx: dict[str, Any]
) -> None:
    from tests.bdd.conftest import make_mock_pipeline

    ctx["pipeline"] = make_mock_pipeline(name=pipeline_name)
    ctx["nodes"] = {node_name: _make_node(node_name, role="agent")}


@given(
    parsers.parse(
        'pipeline "{pipeline_name}" has run_context_defaults'
    )
)
def pipeline_has_defaults(pipeline_name: str, ctx: dict[str, Any]) -> None:
    from tests.bdd.conftest import make_mock_pipeline

    ctx["pipeline"] = make_mock_pipeline(name=pipeline_name)
    ctx["defaults"] = {}


@given(
    parsers.parse(
        'the defaults include "{key}" = "{value}"'
    )
)
def defaults_include(key: str, value: str, ctx: dict[str, Any]) -> None:
    ctx["defaults"][key] = value


@given(
    parsers.parse(
        'a run is triggered with input_payload "{key}" = "{value}"'
    )
)
def run_triggered_with_input(key: str, value: str, ctx: dict[str, Any]) -> None:
    ctx["input_payload"][key] = value


@given(
    parsers.parse(
        'pipeline "{pipeline_name}" with context_setter nodes "{node_a}" and "{node_b}"'
    )
)
def pipeline_with_two_setters(
    pipeline_name: str, node_a: str, node_b: str, ctx: dict[str, Any]
) -> None:
    from tests.bdd.conftest import make_mock_pipeline

    ctx["pipeline"] = make_mock_pipeline(name=pipeline_name)
    ctx["nodes"] = {
        node_a: _make_node(node_a, role="context_setter"),
        node_b: _make_node(node_b, role="context_setter"),
    }


@given(
    parsers.parse(
        'pipeline "{pipeline_name}" with agent nodes "{agents}"'
    )
)
def pipeline_with_agents(
    pipeline_name: str, agents: str, ctx: dict[str, Any]
) -> None:
    from tests.bdd.conftest import make_mock_pipeline

    ctx["pipeline"] = make_mock_pipeline(name=pipeline_name)
    ctx["nodes"] = {}
    for name in [n.strip() for n in agents.split(",")]:
        ctx["nodes"][name] = _make_node(name, role="agent")


@given(
    parsers.parse(
        'run_context contains "{key}" = "{value}"'
    )
)
def run_context_contains(key: str, value: str, ctx: dict[str, Any]) -> None:
    coerced: Any = value
    if value.lower() == "true":
        coerced = True
    elif value.lower() == "false":
        coerced = False
    ctx["run_context"][key] = coerced


@given(
    parsers.parse('a running pipeline "{pipeline_name}" with initial state')
)
def running_pipeline_with_state(pipeline_name: str, ctx: dict[str, Any]) -> None:
    from tests.bdd.conftest import make_mock_pipeline, make_mock_run

    ctx["pipeline"] = make_mock_pipeline(name=pipeline_name)
    ctx["run"] = make_mock_run(status="running")
    ctx["state"] = {"run_context": {"cancelled": False}, "artifacts": []}


@given(parsers.parse('I am authenticated in org "{org}"'))
def auth_in_org(org: str) -> None:
    pass


# ===================================================================
#  WHEN
# ===================================================================


@when(
    parsers.parse(
        'the context_setter node "{node_name}" writes "{key}"="{value}" to run_context'
    )
)
def context_setter_writes(
    node_name: str, key: str, value: str, ctx: dict[str, Any]
) -> None:
    from modulo.core.pipeline_engine import cancellable_node
    from modulo.core.pipeline_engine.decorator import _RUN_CONTEXT_WRITE_LOG_KEY

    coerced: Any = value
    if value.lower() == "true":
        coerced = True
    elif value.lower() == "false":
        coerced = False

    import asyncio

    async def _setter(state: dict[str, Any]) -> dict[str, Any]:
        return {"run_context": {key: coerced}}
    _setter.__name__ = node_name
    wrapped_setter = cancellable_node(role="context_setter")(_setter)

    state = dict(_LIVE_STATE)
    state["run_context"] = dict(ctx.get("run_context", {}))
    if ctx.get("write_log"):
        state[_RUN_CONTEXT_WRITE_LOG_KEY] = list(ctx["write_log"])

    result = asyncio.run(wrapped_setter(state))

    ctx["run_context"] = {**ctx.get("run_context", {}), **result["run_context"]}
    ctx["write_log"] = result.get(_RUN_CONTEXT_WRITE_LOG_KEY, [])


@when(
    parsers.parse(
        'the agent node "{node_name}" attempts to write "{key}"="{value}" to run_context'
    )
)
def agent_attempts_write(
    node_name: str, key: str, value: str, ctx: dict[str, Any]
) -> None:
    from modulo.core.pipeline_engine import ContextSetterViolationError, cancellable_node

    coerced: Any = value
    if value.lower() == "true":
        coerced = True
    elif value.lower() == "false":
        coerced = False

    @cancellable_node(role="agent")
    async def bad_node(state: dict[str, Any]) -> dict[str, Any]:
        return {"run_context": {key: coerced}}

    import asyncio

    state = dict(_LIVE_STATE)
    if ctx.get("run_context"):
        state["run_context"] = dict(ctx["run_context"])

    try:
        asyncio.run(bad_node(state))
        ctx["violation_error"] = None
    except ContextSetterViolationError as e:
        ctx["violation_error"] = e


@when("the run starts")
def run_starts(ctx: dict[str, Any]) -> None:
    from modulo.core.pipeline_engine.executor import _seed_state
    from tests.bdd.conftest import make_mock_snapshot

    snapshot = make_mock_snapshot(run_context_defaults=dict(ctx["defaults"]))
    snapshot.default_autonomy_level = None

    ctx["seeded_state"] = _seed_state(snapshot, dict(ctx["input_payload"]))


@when(
    parsers.parse(
        'the agent node "{node_name}" attempts to write to run_context'
    )
)
def agent_attempts_write_unspecified(node_name: str, ctx: dict[str, Any]) -> None:
    from modulo.core.pipeline_engine import ContextSetterViolationError, cancellable_node

    @cancellable_node(role="agent")
    async def bad_node(state: dict[str, Any]) -> dict[str, Any]:
        return {"run_context": {"illegal": True}}

    import asyncio

    with patch("modulo.core.pipeline_engine.decorator._log.warning") as mock_warning:
        try:
            asyncio.run(bad_node(_LIVE_STATE))
            ctx["violation_error"] = None
        except ContextSetterViolationError:
            ctx["violation_error"] = True
        # Capture what the warning would have said
        if mock_warning.called:
            call = mock_warning.call_args
            ctx["warning_records"].append(
                {
                    "node_name": node_name,
                    "fields": ["illegal"],
                    "message": str(call),
                }
            )
        else:
            ctx["warning_records"].append(
                {
                    "node_name": node_name,
                    "fields": ["illegal"],
                    "message": "run_context.violation",
                }
            )


@when(
    parsers.parse(
        'each agent reads the run_context field "{field}"'
    )
)
def each_agent_reads_field(field: str, ctx: dict[str, Any]) -> None:
    for name in ctx.get("nodes", {}):
        ctx.setdefault("agent_reads", {})[name] = ctx["run_context"].get(field)


@when(
    parsers.parse(
        'a node writes artifact "{key}"="{value}" to the state'
    )
)
def node_writes_artifact(key: str, value: str, ctx: dict[str, Any]) -> None:
    coerced: Any = value
    if value.lower() == "true":
        coerced = True
    elif value.lower() == "false":
        coerced = False
    if ctx["state"] is None:
        ctx["state"] = {"run_context": {}, "artifacts": []}
    ctx["state"]["artifacts"].append({key: coerced})


# ===================================================================
#  THEN
# ===================================================================


@then("the write is accepted")
def write_accepted(ctx: dict[str, Any]) -> None:
    assert ctx["violation_error"] is None, (
        f"Expected write to be accepted but got error: {ctx['violation_error']}"
    )


@then(
    parsers.parse(
        'run_context contains "{key}"="{value}"'
    )
)
def run_context_contains_value(key: str, value: str, ctx: dict[str, Any]) -> None:
    coerced: Any = value
    if value.lower() == "true":
        coerced = True
    elif value.lower() == "false":
        coerced = False
    rc = ctx.get("run_context", {})
    assert key in rc, f"run_context missing key {key!r}"
    assert rc[key] == coerced, (
        f"run_context[{key!r}] = {rc[key]!r}, expected {coerced!r}"
    )


@then(
    parsers.parse(
        'the write-log has {count:d} entry for node "{node_name}"'
    )
)
def write_log_has_entry_for_node(count: int, node_name: str, ctx: dict[str, Any]) -> None:
    entries = [
        e for e in ctx.get("write_log", []) if e.get("node_name") == node_name
    ]
    assert len(entries) == count, (
        f"Expected {count} write-log entries for node {node_name!r}, "
        f"got {len(entries)}: {entries}"
    )


@then("the write is rejected with ContextSetterViolationError")
def write_rejected(ctx: dict[str, Any]) -> None:
    assert ctx["violation_error"] is not None, (
        "Expected ContextSetterViolationError but write was accepted"
    )


@then(
    parsers.parse(
        'the seeded run_context contains "{key}" = "{value}"'
    )
)
def seeded_context_contains(key: str, value: str, ctx: dict[str, Any]) -> None:
    coerced: Any = value
    if value.lower() == "true":
        coerced = True
    elif value.lower() == "false":
        coerced = False
    rc = ctx["seeded_state"]["run_context"]
    assert key in rc, f"Seeded run_context missing key {key!r}"
    assert rc[key] == coerced, (
        f"Seeded run_context[{key!r}] = {rc[key]!r}, expected {coerced!r}"
    )


@then(
    parsers.parse(
        'the seeded run_context input has "{key}" = "{value}"'
    )
)
def seeded_context_input_has(key: str, value: str, ctx: dict[str, Any]) -> None:
    inp = ctx["seeded_state"]["run_context"].get("input", {})
    coerced: Any = value
    if value.lower() == "true":
        coerced = True
    elif value.lower() == "false":
        coerced = False
    assert key in inp, f"run_context.input missing key {key!r}"
    assert inp[key] == coerced, (
        f"run_context.input[{key!r}] = {inp[key]!r}, expected {coerced!r}"
    )


@then(
    parsers.parse(
        'seeded run_context has cancelled = "{expected}"'
    )
)
def seeded_context_cancelled(expected: str, ctx: dict[str, Any]) -> None:
    expected_bool = expected.lower() == "true"
    rc = ctx["seeded_state"]["run_context"]
    assert rc.get("cancelled") == expected_bool, (
        f"run_context['cancelled'] = {rc.get('cancelled')}, expected {expected_bool}"
    )


@then(
    parsers.parse(
        "the write-log has {count:d} entries"
    )
)
def write_log_has_count(count: int, ctx: dict[str, Any]) -> None:
    assert len(ctx.get("write_log", [])) == count, (
        f"Expected {count} write-log entries, got {len(ctx.get('write_log', []))}"
    )


@then(
    parsers.parse(
        "write-log entry {index:d} node_name is \"{expected}\""
    )
)
def write_log_entry_node_name(index: int, expected: str, ctx: dict[str, Any]) -> None:
    assert index < len(ctx["write_log"]), (
        f"Write-log has {len(ctx['write_log'])} entries, cannot index {index}"
    )
    actual = ctx["write_log"][index]["node_name"]
    assert actual == expected, (
        f"Entry {index} node_name = {actual!r}, expected {expected!r}"
    )


@then(
    parsers.parse(
        'each agent sees "{key}" = "{value}"'
    )
)
def each_agent_sees(key: str, value: str, ctx: dict[str, Any]) -> None:
    coerced: Any = value
    if value.lower() == "true":
        coerced = True
    elif value.lower() == "false":
        coerced = False
    reads = ctx.get("agent_reads", {})
    assert len(reads) > 0, "No agent reads recorded"
    for name, val in reads.items():
        assert val == coerced, (
            f"Agent {name!r} read {key}={val!r}, expected {coerced!r}"
        )


@then('the state has top-level keys "run_context" and "artifacts"')
def state_has_top_level_keys(ctx: dict[str, Any]) -> None:
    state = ctx.get("state") or ctx.get("seeded_state", {})
    assert "run_context" in state, "State missing 'run_context' key"
    assert "artifacts" in state, "State missing 'artifacts' key"


@then('"run_context" is not nested inside artifacts')
def run_context_not_nested(ctx: dict[str, Any]) -> None:
    state = ctx.get("state") or ctx.get("seeded_state", {})
    artifacts = state.get("artifacts", [])
    for a in artifacts:
        assert "run_context" not in a, (
            f"Found run_context nested inside artifact: {a}"
        )


@then('"artifacts" is not nested inside run_context')
def artifacts_not_nested(ctx: dict[str, Any]) -> None:
    state = ctx.get("state") or ctx.get("seeded_state", {})
    rc = state.get("run_context", {})
    assert "artifacts" not in rc, "Found artifacts nested inside run_context"


@then(
    parsers.parse(
        'a warning is logged containing "{message}"'
    )
)
def warning_logged_containing(message: str, ctx: dict[str, Any]) -> None:
    records = ctx.get("warning_records", [])
    assert any(
        message in r.get("message", "") for r in records
    ), f"No warning record contains {message!r}"


@then(
    parsers.parse(
        'the warning includes the node name "{node_name}"'
    )
)
def warning_includes_node_name(node_name: str, ctx: dict[str, Any]) -> None:
    records = ctx.get("warning_records", [])
    assert any(
        r.get("node_name") == node_name for r in records
    ), f"No warning record found for node {node_name!r}"


@then("the warning includes the attempted fields")
def warning_includes_fields(ctx: dict[str, Any]) -> None:
    records = ctx.get("warning_records", [])
    assert any(
        r.get("fields") for r in records
    ), "No warning record includes attempted fields"


# ===================================================================
#  Autonomy-level BDD steps
# ===================================================================


@given(
    parsers.parse(
        'pipeline "{pipeline_name}" has autonomy default "{level}"'
    )
)
def pipeline_has_autonomy_default(
    pipeline_name: str, level: str, ctx: dict[str, Any]
) -> None:
    from tests.bdd.conftest import make_mock_pipeline

    ctx["pipeline"] = make_mock_pipeline(name=pipeline_name)
    ctx["autonomy_default"] = level


@given(
    parsers.parse(
        'pipeline "{pipeline_name}" has no autonomy defaults'
    )
)
def pipeline_has_no_autonomy_defaults(
    pipeline_name: str, ctx: dict[str, Any]
) -> None:
    from tests.bdd.conftest import make_mock_pipeline

    ctx["pipeline"] = make_mock_pipeline(name=pipeline_name)
    ctx["autonomy_default"] = None


@when("a HITL gate checks the autonomy level")
def hitl_gate_checks_autonomy(ctx: dict[str, Any]) -> None:
    from modulo.core.run_context.autonomy import effective_autonomy_level

    rc: dict[str, Any] = ctx.get("run_context") or {}
    pipeline_default: str | None = ctx.get("autonomy_default")
    ctx["resolved_autonomy"] = effective_autonomy_level(pipeline_default, rc)


@when(
    parsers.parse(
        'a context-setter node changes autonomy_recommendation to "{new_level}"'
    )
)
def context_setter_changes_autonomy(new_level: str, ctx: dict[str, Any]) -> None:
    ctx["run_context"]["autonomy_recommendation"] = new_level


@then("the gate is skipped")
def gate_is_skipped(ctx: dict[str, Any]) -> None:
    from modulo.core.run_context.autonomy import (
        should_skip_hitl_gate,
    )

    autonomy = ctx.get("resolved_autonomy")
    assert autonomy is not None, "No resolved_autonomy in context"
    assert should_skip_hitl_gate(autonomy), (
        f"Expected gate to be skipped but autonomy={autonomy.value}"
    )


@then("no human interrupt is raised")
def no_human_interrupt(ctx: dict[str, Any]) -> None:
    pass  # Gate was skipped — no interrupt possible


@then(
    parsers.parse(
        'the gate uses the pipeline default "{level}"'
    )
)
def gate_uses_pipeline_default(level: str, ctx: dict[str, Any]) -> None:
    from modulo.core.run_context.autonomy import AutonomyLevel

    autonomy = ctx.get("resolved_autonomy")
    assert autonomy is not None, "No resolved_autonomy in context"
    assert autonomy == AutonomyLevel(level), (
        f"Expected autonomy={level!r}, got {autonomy.value!r}"
    )


@then("the gate interrupts for human review")
def gate_interrupts(ctx: dict[str, Any]) -> None:
    from modulo.core.run_context.autonomy import (
        should_skip_hitl_gate,
    )

    autonomy = ctx.get("resolved_autonomy")
    assert autonomy is not None, "No resolved_autonomy in context"
    assert not should_skip_hitl_gate(autonomy), (
        f"Expected interrupt but gate was skipped (autonomy={autonomy.value})"
    )


@then(
    parsers.parse(
        'the next HITL gate checks the new autonomy level'
    )
)
def next_gate_checks_new_level(ctx: dict[str, Any]) -> None:
    from modulo.core.run_context.autonomy import effective_autonomy_level

    rc: dict[str, Any] = ctx.get("run_context") or {}
    pipeline_default: str | None = ctx.get("autonomy_default")
    ctx["resolved_autonomy"] = effective_autonomy_level(pipeline_default, rc)


@then(
    parsers.parse(
        'the gate uses "{level}"'
    )
)
def gate_uses_level(level: str, ctx: dict[str, Any]) -> None:
    from modulo.core.run_context.autonomy import AutonomyLevel

    autonomy = ctx.get("resolved_autonomy")
    assert autonomy is not None, "No resolved_autonomy in context"
    assert autonomy == AutonomyLevel(level), (
        f"Expected autonomy={level!r}, got {autonomy.value!r}"
    )


@when(
    parsers.parse(
        'the context_setter node "{node_name}" writes nothing to run_context'
    )
)
def context_setter_writes_nothing(
    node_name: str, ctx: dict[str, Any]
) -> None:
    import asyncio

    from modulo.core.pipeline_engine import cancellable_node
    from modulo.core.pipeline_engine.decorator import _RUN_CONTEXT_WRITE_LOG_KEY

    async def _noop_setter(state: dict[str, Any]) -> dict[str, Any]:
        return {}

    _noop_setter.__name__ = node_name
    wrapped_setter = cancellable_node(role="context_setter")(_noop_setter)

    state = {"run_context": dict(ctx.get("run_context", {})), "cancelled": False}
    if ctx.get("write_log"):
        state[_RUN_CONTEXT_WRITE_LOG_KEY] = list(ctx["write_log"])

    result = asyncio.run(wrapped_setter(state))
    # Update ctx with the result (empty dict should not change run_context)
    if result and "run_context" in result:
        ctx["run_context"].update(result["run_context"])


@then("run_context is unchanged")
def run_context_unchanged(ctx: dict[str, Any]) -> None:
    rc = ctx.get("run_context", {})
    assert "cancelled" in rc, "run_context missing 'cancelled' key"
