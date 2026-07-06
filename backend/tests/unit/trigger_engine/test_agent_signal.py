"""Unit tests for agent_signal trigger — fire_agent_signal and helpers."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.trigger_engine.agent_signal import fire_agent_signal


def _make_trigger(
    *,
    trigger_id: uuid.UUID | None = None,
    pipeline_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    source_pipeline_id: uuid.UUID | None = None,
    source_node_id: str = "node-1",
    active: bool = True,
    max_concurrent_runs: int = 5,
    snapshot_id: str | None = None,
) -> MagicMock:
    """Build a mock Trigger for testing."""
    tid = trigger_id or uuid.uuid4()
    pid = pipeline_id or uuid.uuid4()
    oid = org_id or uuid.uuid4()
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


@pytest.fixture
def mock_session() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    return session


def _setup_session(session: MagicMock, triggers: list[Any], count: int = 0) -> None:
    """Set up session.execute() with trigger query and count query.

    The first call returns trigger results; subsequent calls return the count.
    This matches the pattern used by ``fire_agent_signal`` which queries
    triggers first, then calls ``_count_active_runs``.
    """
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


@pytest.fixture
def mock_create_run() -> Any:
    with patch("modulo.core.trigger_engine.agent_signal.create_run", new_callable=AsyncMock) as m:
        m.return_value = MagicMock(id=uuid.uuid4())
        yield m


# ---------------------------------------------------------------------------
# fire_agent_signal — core function tests
# ---------------------------------------------------------------------------


class TestFireAgentSignal:
    """Tests for the main fire_agent_signal() function."""

    async def test_fires_child_run_on_matching_trigger(
        self,
        mock_session: MagicMock,
        mock_create_run: AsyncMock,
    ) -> None:
        """A matching agent_signal trigger should create a child run."""
        org_id = uuid.uuid4()
        source_pipeline_id = uuid.uuid4()
        source_run_id = uuid.uuid4()
        trigger = _make_trigger(
            org_id=org_id,
            source_pipeline_id=source_pipeline_id,
            source_node_id="my-node",
        )
        _setup_session(mock_session, [trigger])

        results = await fire_agent_signal(
            mock_session,
            org_id=org_id,
            source_run_id=source_run_id,
            source_pipeline_id=source_pipeline_id,
            completed_node_id="my-node",
            node_output={"result": "ok"},
        )

        assert len(results) == 1
        assert results[0]["status"] == "fired"
        assert results[0]["trigger_id"] == str(trigger.id)
        mock_create_run.assert_awaited_once()
        call_kwargs = mock_create_run.call_args[1]
        assert call_kwargs["org_id"] == org_id
        assert call_kwargs["pipeline_id"] == trigger.pipeline_id
        assert call_kwargs["trigger_type"] == "agent_signal"
        assert call_kwargs["parent_run_id"] == source_run_id
        assert call_kwargs["input_payload"]["source_run_id"] == str(source_run_id)
        assert call_kwargs["input_payload"]["node_output"]["result"] == "ok"

    async def test_no_matching_triggers_returns_empty(
        self,
        mock_session: MagicMock,
        mock_create_run: AsyncMock,
    ) -> None:
        """No agent_signal triggers at all should return empty list."""
        _setup_session(mock_session, [])

        results = await fire_agent_signal(
            mock_session,
            org_id=uuid.uuid4(),
            source_run_id=uuid.uuid4(),
            source_pipeline_id=uuid.uuid4(),
            completed_node_id="node-1",
        )

        assert results == []
        mock_create_run.assert_not_called()

    async def test_skips_non_matching_source_pipeline(
        self,
        mock_session: MagicMock,
        mock_create_run: AsyncMock,
    ) -> None:
        """Trigger watching a different pipeline should be skipped."""
        org_id = uuid.uuid4()
        trigger = _make_trigger(
            org_id=org_id,
            source_pipeline_id=uuid.uuid4(),  # different pipeline
            source_node_id="node-1",
        )
        _setup_session(mock_session, [trigger])

        results = await fire_agent_signal(
            mock_session,
            org_id=org_id,
            source_run_id=uuid.uuid4(),
            source_pipeline_id=uuid.uuid4(),  # different from trigger's source
            completed_node_id="node-1",
        )

        assert results == []
        mock_create_run.assert_not_called()

    async def test_skips_non_matching_node_id(
        self,
        mock_session: MagicMock,
        mock_create_run: AsyncMock,
    ) -> None:
        """Trigger watching a different node should be skipped."""
        org_id = uuid.uuid4()
        source_pipeline_id = uuid.uuid4()
        trigger = _make_trigger(
            org_id=org_id,
            source_pipeline_id=source_pipeline_id,
            source_node_id="watched-node",
        )
        _setup_session(mock_session, [trigger])

        results = await fire_agent_signal(
            mock_session,
            org_id=org_id,
            source_run_id=uuid.uuid4(),
            source_pipeline_id=source_pipeline_id,
            completed_node_id="different-node",
        )

        assert results == []
        mock_create_run.assert_not_called()

    async def test_skips_inactive_triggers(
        self,
        mock_session: MagicMock,
        mock_create_run: AsyncMock,
    ) -> None:
        """An inactive trigger should not fire because WHERE active=True excludes it."""
        org_id = uuid.uuid4()
        _setup_session(mock_session, [])

        results = await fire_agent_signal(
            mock_session,
            org_id=org_id,
            source_run_id=uuid.uuid4(),
            source_pipeline_id=uuid.uuid4(),
            completed_node_id="node-1",
        )

        assert results == []
        mock_create_run.assert_not_called()

    async def test_concurrency_limit_skips_fire(
        self,
        mock_session: MagicMock,
        mock_create_run: AsyncMock,
    ) -> None:
        """When child pipeline has too many active runs, skip firing."""
        org_id = uuid.uuid4()
        source_pipeline_id = uuid.uuid4()
        trigger = _make_trigger(
            org_id=org_id,
            source_pipeline_id=source_pipeline_id,
            source_node_id="node-1",
            max_concurrent_runs=1,
        )
        _setup_session(mock_session, [trigger], count=1)

        results = await fire_agent_signal(
            mock_session,
            org_id=org_id,
            source_run_id=uuid.uuid4(),
            source_pipeline_id=source_pipeline_id,
            completed_node_id="node-1",
        )

        assert len(results) == 1
        assert results[0]["status"] == "skipped"
        assert results[0]["reason"] == "concurrency_limit"
        mock_create_run.assert_not_called()

    async def test_fires_without_node_output(
        self,
        mock_session: MagicMock,
        mock_create_run: AsyncMock,
    ) -> None:
        """Should fire even when node_output is None."""
        org_id = uuid.uuid4()
        source_pipeline_id = uuid.uuid4()
        trigger = _make_trigger(
            org_id=org_id,
            source_pipeline_id=source_pipeline_id,
            source_node_id="my-node",
        )
        _setup_session(mock_session, [trigger])

        results = await fire_agent_signal(
            mock_session,
            org_id=org_id,
            source_run_id=uuid.uuid4(),
            source_pipeline_id=source_pipeline_id,
            completed_node_id="my-node",
            node_output=None,
        )

        assert len(results) == 1
        assert results[0]["status"] == "fired"
        mock_create_run.assert_awaited_once()
        input_payload = mock_create_run.call_args[1]["input_payload"]
        assert "node_output" not in input_payload

    async def test_fires_multiple_triggers_on_same_node(
        self,
        mock_session: MagicMock,
        mock_create_run: AsyncMock,
    ) -> None:
        """Multiple triggers watching the same node should all fire."""
        org_id = uuid.uuid4()
        source_pipeline_id = uuid.uuid4()
        trigger_a = _make_trigger(
            trigger_id=uuid.uuid4(),
            org_id=org_id,
            source_pipeline_id=source_pipeline_id,
            source_node_id="shared-node",
            pipeline_id=uuid.uuid4(),
        )
        trigger_b = _make_trigger(
            trigger_id=uuid.uuid4(),
            org_id=org_id,
            source_pipeline_id=source_pipeline_id,
            source_node_id="shared-node",
            pipeline_id=uuid.uuid4(),
        )
        _setup_session(mock_session, [trigger_a, trigger_b])

        results = await fire_agent_signal(
            mock_session,
            org_id=org_id,
            source_run_id=uuid.uuid4(),
            source_pipeline_id=source_pipeline_id,
            completed_node_id="shared-node",
        )

        assert len(results) == 2
        assert all(r["status"] == "fired" for r in results)
        assert mock_create_run.await_count == 2
