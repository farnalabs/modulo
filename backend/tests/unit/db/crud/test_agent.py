"""Unit tests for Agent CRUD operations (mocked session, no DB)."""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.crud.base import PageResult

_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_AGENT_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
_ACCOUNT_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


@pytest.fixture
def mock_session() -> AsyncMock:
    return AsyncMock(spec=AsyncSession)


def _make_agent(**overrides: Any) -> MagicMock:
    agent = MagicMock()
    agent.id = _AGENT_ID
    agent.organisation_id = _ORG_ID
    agent.name = overrides.get("name", "planner")
    agent.prompt_template = overrides.get("prompt_template", "original prompt")
    agent.prompt_version_history = overrides.get("history", [])
    agent.updated_at = overrides.get("updated_at", datetime(2026, 1, 1, tzinfo=UTC))
    agent.eval_id = None
    agent.__table__ = MagicMock()
    return agent


def _exec_result(scalar_value: Any = None, scalars_list: list[Any] | None = None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=scalar_value)
    result.scalar_one = MagicMock(return_value=1)
    result.scalar = MagicMock(return_value=scalar_value)
    scalars = MagicMock()
    scalars.all = MagicMock(return_value=scalars_list or [])
    result.scalars = MagicMock(return_value=scalars)
    return result


class TestCreateAgent:
    async def test_creates_agent_with_org_id_and_defaults(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.agent.Agent", return_value=_make_agent()) as mock_agent_cls:
            from modulo.db.crud.agent import create_agent

            result = await create_agent(
                mock_session,
                org_id=_ORG_ID,
                name="planner",
                account_id=_ACCOUNT_ID,
                prompt_template="hello",
            )

        kwargs = mock_agent_cls.call_args.kwargs
        assert kwargs["organisation_id"] == _ORG_ID
        assert kwargs["account_id"] == _ACCOUNT_ID
        assert kwargs["prompt_template"] == "hello"
        assert not kwargs["connector_type_refs"]
        assert not kwargs["retry_policy"]
        assert kwargs["input_schema_version"] == "latest"
        mock_session.add.assert_called_once()
        mock_session.flush.assert_awaited_once()
        assert result is not None

    async def test_create_agent_keeps_explicit_lists(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.agent.Agent", return_value=_make_agent()) as mock_agent_cls:
            from modulo.db.crud.agent import create_agent

            await create_agent(
                mock_session,
                org_id=_ORG_ID,
                name="planner",
                account_id=_ACCOUNT_ID,
                prompt_template="hello",
                connector_type_refs=[{"type": "github"}],
                evals=[{"id": 1}],
                retry_policy={"attempts": 2},
                template_id="not-a-uuid",
            )

        kwargs = mock_agent_cls.call_args.kwargs
        assert kwargs["connector_type_refs"] == [{"type": "github"}]
        assert kwargs["retry_policy"] == {"attempts": 2}


class TestGetAgent:
    async def test_returns_agent_when_found(self, mock_session: AsyncMock) -> None:
        agent = _make_agent()
        mock_session.execute = AsyncMock(return_value=_exec_result(scalar_value=agent))
        from modulo.db.crud.agent import get_agent

        result = await get_agent(mock_session, _AGENT_ID)
        assert result is agent

    async def test_returns_none_when_not_found(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_result(scalar_value=None))
        from modulo.db.crud.agent import get_agent

        result = await get_agent(mock_session, _AGENT_ID)
        assert result is None


class TestListAgents:
    async def test_returns_paginated_agents(self, mock_session: AsyncMock) -> None:
        agents = [_make_agent(), _make_agent(name="worker")]
        count_result = _exec_result()
        count_result.scalar_one = MagicMock(return_value=5)
        listing_result = MagicMock()
        listing_result.scalars = MagicMock(return_value=list(agents))
        mock_session.execute = AsyncMock(side_effect=[count_result, listing_result])

        from modulo.db.crud.agent import list_agents

        result = await list_agents(mock_session, page=2, page_size=2)
        assert isinstance(result, PageResult)
        assert result.total == 5
        assert result.page == 2
        assert result.page_size == 2
        assert result.items == agents

    async def test_returns_empty_page_on_programming_error(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=ProgrammingError("missing", None, Exception("boom")))
        from modulo.db.crud.agent import list_agents

        result = await list_agents(mock_session, page=1, page_size=20)
        assert isinstance(result, PageResult)
        assert result.total == 0
        assert not result.items


class TestUpdateDeleteAgent:
    async def test_update_agent_applies_updates(self, mock_session: AsyncMock) -> None:
        agent = _make_agent(name="planner")
        with (
            patch("modulo.db.crud.agent.get_agent", AsyncMock(return_value=agent)),
            patch("modulo.db.crud.agent.apply_updates") as apply_updates,
        ):
            from modulo.db.crud.agent import update_agent

            result = await update_agent(mock_session, _AGENT_ID, {"name": "renamed"})
        apply_updates.assert_called_once_with(agent, {"name": "renamed"})
        mock_session.flush.assert_awaited_once()
        assert result is agent

    async def test_update_agent_returns_none_when_missing(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.agent.get_agent", AsyncMock(return_value=None)):
            from modulo.db.crud.agent import update_agent

            result = await update_agent(mock_session, _AGENT_ID, {"name": "renamed"})
        assert result is None

    async def test_delete_agent_returns_true(self, mock_session: AsyncMock) -> None:
        agent = _make_agent()
        with patch("modulo.db.crud.agent.get_agent", AsyncMock(return_value=agent)):
            from modulo.db.crud.agent import delete_agent

            result = await delete_agent(mock_session, _AGENT_ID)
        assert result is True
        mock_session.delete.assert_awaited_once_with(agent)

    async def test_delete_agent_returns_false_when_missing(self, mock_session: AsyncMock) -> None:
        with patch("modulo.db.crud.agent.get_agent", AsyncMock(return_value=None)):
            from modulo.db.crud.agent import delete_agent

            result = await delete_agent(mock_session, _AGENT_ID)
        assert result is False


class TestAddPromptVersion:
    async def test_appends_history_and_updates_template(self, mock_session: AsyncMock) -> None:
        agent = _make_agent(history=[{"version": "v1", "template": "original"}])
        mock_session.execute = AsyncMock(return_value=_exec_result(scalar_value=agent))
        from modulo.db.crud.agent import add_prompt_version

        before = datetime.now(UTC)
        result = await add_prompt_version(mock_session, _AGENT_ID, new_template="new prompt", notes="tuned")

        history = result.prompt_version_history
        assert len(history) == 2
        latest = history[-1]
        assert latest["version"] == "v2"
        assert latest["template"] == "original prompt"
        assert latest["notes"] == "tuned"
        assert isinstance(latest["created_at"], str)
        assert result.prompt_template == "new prompt"
        mock_session.flush.assert_awaited_once()
        assert isinstance(before, datetime)

    async def test_appends_with_version_label_and_eval_refs(self, mock_session: AsyncMock) -> None:
        agent = _make_agent()
        mock_session.execute = AsyncMock(return_value=_exec_result(scalar_value=agent))
        from modulo.db.crud.agent import add_prompt_version

        er_id = uuid.uuid4()
        result = await add_prompt_version(
            mock_session,
            _AGENT_ID,
            new_template="t2",
            version_label="vX",
            optimized_from="opt-job",
            eval_result_ids=[er_id],
        )
        latest = result.prompt_version_history[-1]
        assert latest["version"] == "vX"
        assert latest["optimized_from"] == "opt-job"
        assert latest["eval_result_ids"] == [str(er_id)]

    async def test_returns_none_when_agent_missing(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_result(scalar_value=None))
        from modulo.db.crud.agent import add_prompt_version

        result = await add_prompt_version(mock_session, _AGENT_ID, new_template="t")
        assert result is None


class TestGetPromptVersion:
    async def test_returns_none_when_agent_missing(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_result(scalar_value=None))
        from modulo.db.crud.agent import get_prompt_version

        assert await get_prompt_version(mock_session, _AGENT_ID, "current") is None

    async def test_current_version_returns_active_template(self, mock_session: AsyncMock) -> None:
        agent = _make_agent()
        mock_session.execute = AsyncMock(return_value=_exec_result(scalar_value=agent))
        from modulo.db.crud.agent import get_prompt_version

        entry = await get_prompt_version(mock_session, _AGENT_ID, "current")
        assert entry is not None
        assert entry["version"] == "current"
        assert entry["template"] == "original prompt"
        assert entry["notes"] == "Current active prompt"
        assert entry["created_at"] == datetime(2026, 1, 1, tzinfo=UTC).isoformat()

    async def test_matching_history_entry_is_returned(self, mock_session: AsyncMock) -> None:
        entry = {"version": "v1", "template": "old"}
        agent = _make_agent(history=[entry])
        mock_session.execute = AsyncMock(return_value=_exec_result(scalar_value=agent))
        from modulo.db.crud.agent import get_prompt_version

        result = await get_prompt_version(mock_session, _AGENT_ID, "v1")
        assert result == entry

    async def test_unknown_version_returns_none(self, mock_session: AsyncMock) -> None:
        agent = _make_agent(history=[{"version": "v1", "template": "old"}])
        mock_session.execute = AsyncMock(return_value=_exec_result(scalar_value=agent))
        from modulo.db.crud.agent import get_prompt_version

        assert await get_prompt_version(mock_session, _AGENT_ID, "v9") is None


class TestRollbackPromptVersion:
    async def test_rolls_back_to_target_version(self, mock_session: AsyncMock) -> None:
        agent = _make_agent(
            history=[
                {"version": "v1", "template": "old prompt"},
                {"version": "v2", "template": "new prompt"},
            ]
        )
        agent.prompt_template = "new prompt"
        mock_session.execute = AsyncMock(return_value=_exec_result(scalar_value=agent))
        from modulo.db.crud.agent import rollback_prompt_version

        result = await rollback_prompt_version(mock_session, _AGENT_ID, "v1")
        assert result.prompt_template == "old prompt"
        history = result.prompt_version_history
        assert len(history) == 3
        latest = history[-1]
        assert latest["version"] == "v3"
        assert latest["template"] == "new prompt"
        assert "Rolled back from v2 to v1" in latest["notes"]
        mock_session.flush.assert_awaited_once()

    async def test_rolls_back_from_current_when_no_history(self, mock_session: AsyncMock) -> None:
        agent = _make_agent(history=[{"version": "v1", "template": "old prompt"}])
        agent.prompt_template = "old prompt"
        mock_session.execute = AsyncMock(return_value=_exec_result(scalar_value=agent))
        from modulo.db.crud.agent import rollback_prompt_version

        result = await rollback_prompt_version(mock_session, _AGENT_ID, "v1")
        assert result is agent
        assert result.prompt_version_history[-1]["notes"] == "Rolled back from v1 to v1"

    async def test_returns_none_when_agent_missing(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(return_value=_exec_result(scalar_value=None))
        from modulo.db.crud.agent import rollback_prompt_version

        assert await rollback_prompt_version(mock_session, _AGENT_ID, "v1") is None

    async def test_returns_none_when_target_missing(self, mock_session: AsyncMock) -> None:
        agent = _make_agent(history=[{"version": "v1", "template": "old"}])
        mock_session.execute = AsyncMock(return_value=_exec_result(scalar_value=agent))
        from modulo.db.crud.agent import rollback_prompt_version

        assert await rollback_prompt_version(mock_session, _AGENT_ID, "v9") is None

    async def test_returns_none_when_target_template_empty(self, mock_session: AsyncMock) -> None:
        agent = _make_agent(history=[{"version": "v1", "template": ""}])
        mock_session.execute = AsyncMock(return_value=_exec_result(scalar_value=agent))
        from modulo.db.crud.agent import rollback_prompt_version

        assert await rollback_prompt_version(mock_session, _AGENT_ID, "v1") is None


class TestGetEvalResultsWithDefs:
    @staticmethod
    def _eval_result(er_id: uuid.UUID, eval_id: uuid.UUID, *, passed: bool = True) -> MagicMock:
        er = MagicMock()
        er.id = er_id
        er.eval_id = eval_id
        er.run_id = uuid.UUID("00000000-0000-0000-0000-00000000000a")
        er.passed = passed
        er.score = 0.9
        er.detail = "ok"
        return er

    @staticmethod
    def _eval_def(def_id: uuid.UUID, *, eval_type: str = "rubric") -> MagicMock:
        ed = MagicMock()
        ed.id = def_id
        ed.name = "def"
        ed.eval_type = eval_type
        ed.config_json = {"k": 1}
        ed.failure_behaviour = "block"
        return ed

    async def test_returns_results_and_definitions(self, mock_session: AsyncMock) -> None:
        er_id = uuid.uuid4()
        eval_id = uuid.UUID("00000000-0000-0000-0000-00000000000b")
        er = self._eval_result(er_id, eval_id)
        ed = self._eval_def(eval_id)

        er_result = MagicMock()
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[er])
        er_result.scalars = MagicMock(return_value=scalars)
        ed_result = MagicMock()
        ed_scalars = MagicMock()
        ed_scalars.all = MagicMock(return_value=[ed])
        ed_result.scalars = MagicMock(return_value=ed_scalars)
        mock_session.execute = AsyncMock(side_effect=[er_result, ed_result])

        from modulo.db.crud.agent import get_eval_results_with_defs

        results_list, definitions = await get_eval_results_with_defs(mock_session, [er_id], _ORG_ID)
        assert results_list == [
            {
                "id": str(er_id),
                "eval_id": str(eval_id),
                "run_id": str(er.run_id),
                "passed": True,
                "score": 0.9,
                "detail": "ok",
            }
        ]
        assert definitions[str(eval_id)]["eval_type"] == "rubric"

    async def test_filters_guardrail_rows(self, mock_session: AsyncMock) -> None:
        guardrail_id = uuid.UUID("00000000-0000-0000-0000-00000000000c")
        rubric_id = uuid.UUID("00000000-0000-0000-0000-00000000000d")
        er_g = self._eval_result(uuid.uuid4(), guardrail_id)
        er_r = self._eval_result(uuid.uuid4(), rubric_id)
        ed_g = self._eval_def(guardrail_id, eval_type="guardrail")
        ed_r = self._eval_def(rubric_id)

        er_result = MagicMock()
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[er_g, er_r])
        er_result.scalars = MagicMock(return_value=scalars)
        ed_result = MagicMock()
        ed_scalars = MagicMock()
        ed_scalars.all = MagicMock(return_value=[ed_g, ed_r])
        ed_result.scalars = MagicMock(return_value=ed_scalars)
        mock_session.execute = AsyncMock(side_effect=[er_result, ed_result])

        from modulo.db.crud.agent import get_eval_results_with_defs

        results_list, _ = await get_eval_results_with_defs(mock_session, [uuid.uuid4()], _ORG_ID)
        assert len(results_list) == 1
        assert results_list[0]["eval_id"] == str(rubric_id)

    async def test_eval_result_table_missing_returns_empty(self, mock_session: AsyncMock) -> None:
        mock_session.execute = AsyncMock(side_effect=ProgrammingError("42P01", None, Exception("missing")))
        from modulo.db.crud.agent import get_eval_results_with_defs

        results_list, definitions = await get_eval_results_with_defs(mock_session, [uuid.uuid4()], _ORG_ID)
        assert results_list == []
        assert definitions == {}

    async def test_eval_def_table_missing_returns_empty(self, mock_session: AsyncMock) -> None:
        er_result = MagicMock()
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[self._eval_result(uuid.uuid4(), uuid.uuid4())])
        er_result.scalars = MagicMock(return_value=scalars)
        mock_session.execute = AsyncMock(
            side_effect=[
                er_result,
                ProgrammingError("42P01", None, Exception("missing")),
            ]
        )
        from modulo.db.crud.agent import get_eval_results_with_defs

        results_list, definitions = await get_eval_results_with_defs(mock_session, [uuid.uuid4()], _ORG_ID)
        assert results_list == []
        assert definitions == {}

    async def test_no_results_returns_empty_lists(self, mock_session: AsyncMock) -> None:
        er_result = MagicMock()
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[])
        er_result.scalars = MagicMock(return_value=scalars)
        ed_result = _exec_result()
        mock_session.execute = AsyncMock(side_effect=[er_result, ed_result])

        from modulo.db.crud.agent import get_eval_results_with_defs

        results_list, definitions = await get_eval_results_with_defs(mock_session, [uuid.uuid4()], _ORG_ID)
        assert results_list == []
        assert definitions == {}

    async def test_runs_through_include_these_helpers(self, mock_session: AsyncMock) -> None:
        agent = _make_agent()
        agent.prompt_version_history = [{"version": "v1", "template": "t"}]
        mock_session.execute = AsyncMock(return_value=_exec_result(scalar_value=agent))
        from modulo.db.crud.agent import get_prompt_version

        assert (await get_prompt_version(mock_session, _AGENT_ID, "v1"))["template"] == "t"
