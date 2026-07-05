"""Unit tests for the in-process scheduler module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


class TestStartSchedulers:
    def test_returns_three_tasks(self):
        """start_schedulers() should return cron, polling, and cleanup tasks."""
        import modulo.core.in_process_scheduler as ips

        mock_engine = MagicMock()
        mock_settings = MagicMock()
        with (
            patch.object(ips, "get_settings", return_value=mock_settings),
            patch.object(ips, "async_sessionmaker", return_value=MagicMock()),
            patch.object(ips, "create_async_engine", return_value=mock_engine),
        ):
            tasks = asyncio.run(ips.start_schedulers(engine=mock_engine))
        assert len(tasks) == 3
        assert tasks[0].get_name() == "cron-scheduler"
        assert tasks[1].get_name() == "polling-scheduler"
        assert tasks[2].get_name() == "cleanup-scheduler"
        for t in tasks:
            t.cancel()

    def test_creates_engine_when_not_provided(self):
        """start_schedulers() should create an engine when not provided."""
        import modulo.core.in_process_scheduler as ips

        mock_settings = MagicMock()
        mock_settings.database_url = "sqlite+aiosqlite:///test.db"

        mock_engine = MagicMock()
        with (
            patch.object(ips, "get_settings", return_value=mock_settings),
            patch.object(ips, "create_async_engine", return_value=mock_engine) as mock_create,
            patch.object(ips, "async_sessionmaker", return_value=MagicMock()),
        ):
            tasks = asyncio.run(ips.start_schedulers())
        assert mock_create.called
        assert len(tasks) == 3
        for t in tasks:
            t.cancel()

    def test_uses_provided_engine(self):
        """start_schedulers() should use the engine provided."""
        import modulo.core.in_process_scheduler as ips

        mock_engine = MagicMock()
        mock_settings = MagicMock()
        with (
            patch.object(ips, "get_settings", return_value=mock_settings),
            patch.object(ips, "create_async_engine") as mock_create,
            patch.object(ips, "async_sessionmaker", return_value=MagicMock()),
        ):
            tasks = asyncio.run(ips.start_schedulers(engine=mock_engine))
        assert not mock_create.called
        assert len(tasks) == 3
        for t in tasks:
            t.cancel()


class TestDisposeSchedulerEngine:
    async def test_disposes_engine(self):
        """dispose_scheduler_engine() should dispose the engine."""
        import modulo.core.in_process_scheduler as ips

        mock_engine = AsyncMock()
        ips._scheduler_engine = mock_engine

        await ips.dispose_scheduler_engine()
        mock_engine.dispose.assert_awaited_once()
        assert ips._scheduler_engine is None

    async def test_safe_when_no_engine(self):
        """dispose_scheduler_engine() should not crash when engine is None."""
        import modulo.core.in_process_scheduler as ips

        ips._scheduler_engine = None
        await ips.dispose_scheduler_engine()

    async def test_logs_on_error(self):
        """dispose_scheduler_engine() should log and not re-raise on error."""
        import modulo.core.in_process_scheduler as ips

        mock_engine = MagicMock()
        mock_engine.dispose = AsyncMock(side_effect=RuntimeError("boom"))
        ips._scheduler_engine = mock_engine

        with patch.object(ips._log, "exception") as mock_log:
            await ips.dispose_scheduler_engine()
        mock_log.assert_called_once()


class TestFetchDueCronTriggers:
    async def test_returns_empty_list_when_no_triggers(self):
        """_fetch_due_cron_triggers should return empty list when no due triggers."""
        import modulo.core.in_process_scheduler as ips

        mock_result = MagicMock()
        mock_result.all.return_value = []

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.execute = AsyncMock(return_value=mock_result)

        factory = MagicMock()
        factory.__aenter__.return_value = factory
        factory.return_value = mock_session

        result = await ips._fetch_due_cron_triggers(factory)
        assert result == []

    async def test_returns_triggers(self):
        """_fetch_due_cron_triggers should return trigger info dicts."""
        import datetime
        import uuid

        import modulo.core.in_process_scheduler as ips

        now = datetime.datetime.now(datetime.UTC)
        trigger_id = uuid.uuid4()
        org_id = uuid.uuid4()
        pipeline_id = uuid.uuid4()

        mock_row = MagicMock()
        mock_row.id = trigger_id
        mock_row.organisation_id = org_id
        mock_row.pipeline_id = pipeline_id
        mock_row.config_json = {"snapshot_id": str(uuid.uuid4())}
        mock_row.cron_expression = "0 6 * * *"
        mock_row.next_fire_at = now

        mock_result = MagicMock()
        mock_result.all.return_value = [mock_row]

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.execute = AsyncMock(return_value=mock_result)

        factory = MagicMock()
        factory.__aenter__.return_value = factory
        factory.return_value = mock_session

        result = await ips._fetch_due_cron_triggers(factory)
        assert len(result) == 1
        assert result[0]["id"] == trigger_id
        assert result[0]["org_id"] == org_id
        assert result[0]["cron_expression"] == "0 6 * * *"


class TestFetchDuePollingTriggers:
    async def test_returns_empty_list_when_no_triggers(self):
        """_fetch_due_polling_triggers should return empty list when no due triggers."""
        import modulo.core.in_process_scheduler as ips

        mock_result = MagicMock()
        mock_result.all.return_value = []

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.execute = AsyncMock(return_value=mock_result)

        factory = MagicMock()
        factory.__aenter__.return_value = factory
        factory.return_value = mock_session

        result = await ips._fetch_due_polling_triggers(factory)
        assert result == []

    async def test_skips_trigger_without_connector_instance_id(self):
        """_fetch_due_polling_triggers should skip triggers with no connector_instance_id."""
        import modulo.core.in_process_scheduler as ips

        mock_row = MagicMock()
        mock_row.config_json = {}

        mock_result = MagicMock()
        mock_result.all.return_value = [mock_row]

        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.execute = AsyncMock(return_value=mock_result)

        factory = MagicMock()
        factory.__aenter__.return_value = factory
        factory.return_value = mock_session

        result = await ips._fetch_due_polling_triggers(factory)
        assert result == []


class TestFireCronWrapper:
    async def test_calls_fire_cron_trigger(self):
        """_fire_cron_wrapper should call fire_cron_trigger on success."""
        import uuid

        import modulo.core.in_process_scheduler as ips

        trigger_id = uuid.uuid4()
        info = {
            "id": trigger_id,
            "org_id": uuid.uuid4(),
            "pipeline_id": uuid.uuid4(),
            "snapshot_id": uuid.uuid4(),
            "cron_expression": "0 6 * * *",
        }

        with (
            patch("modulo.core.in_process_scheduler.fire_cron_trigger", new_callable=AsyncMock) as mock_fire,
        ):
            mock_fire.return_value = {"status": "fired", "run_id": str(uuid.uuid4())}
            factory = MagicMock()
            await ips._fire_cron_wrapper(factory, info)
        mock_fire.assert_awaited_once_with(
            trigger_id=info["id"],
            org_id=info["org_id"],
            pipeline_id=info["pipeline_id"],
            snapshot_id=info["snapshot_id"],
            cron_expression=info["cron_expression"],
        )

    async def test_logs_on_error(self):
        """_fire_cron_wrapper should log exceptions."""
        import uuid

        import modulo.core.in_process_scheduler as ips

        info = {
            "id": uuid.uuid4(),
            "org_id": uuid.uuid4(),
            "pipeline_id": uuid.uuid4(),
            "snapshot_id": uuid.uuid4(),
            "cron_expression": "0 6 * * *",
        }

        with (
            patch("modulo.core.in_process_scheduler.fire_cron_trigger", new_callable=AsyncMock) as mock_fire,
            patch.object(ips._log, "exception") as mock_log,
        ):
            mock_fire.side_effect = RuntimeError("boom")
            factory = MagicMock()
            await ips._fire_cron_wrapper(factory, info)
        mock_log.assert_called_once()


class TestFirePollingWrapper:
    async def test_calls_fire_polling_trigger(self):
        """_fire_polling_wrapper should call fire_polling_trigger on success."""
        import uuid

        import modulo.core.in_process_scheduler as ips

        trigger_id = uuid.uuid4()
        info = {
            "id": trigger_id,
            "org_id": uuid.uuid4(),
            "pipeline_id": uuid.uuid4(),
            "snapshot_id": uuid.uuid4(),
            "connector_instance_id": uuid.uuid4(),
            "poll_query": "select * from issues",
            "condition_expression": None,
        }

        with (
            patch("modulo.core.in_process_scheduler.fire_polling_trigger", new_callable=AsyncMock) as mock_fire,
        ):
            mock_fire.return_value = {"status": "fired", "run_id": str(uuid.uuid4())}
            factory = MagicMock()
            await ips._fire_polling_wrapper(factory, info)
        mock_fire.assert_awaited_once_with(
            trigger_id=info["id"],
            org_id=info["org_id"],
            pipeline_id=info["pipeline_id"],
            connector_instance_id=info["connector_instance_id"],
            poll_query=info["poll_query"],
            condition_expression=info.get("condition_expression"),
        )

    async def test_logs_on_error(self):
        """_fire_polling_wrapper should log exceptions."""
        import uuid

        import modulo.core.in_process_scheduler as ips

        info = {
            "id": uuid.uuid4(),
            "org_id": uuid.uuid4(),
            "pipeline_id": uuid.uuid4(),
            "snapshot_id": uuid.uuid4(),
            "connector_instance_id": uuid.uuid4(),
            "poll_query": "select * from issues",
            "condition_expression": None,
        }

        with (
            patch("modulo.core.in_process_scheduler.fire_polling_trigger", new_callable=AsyncMock) as mock_fire,
            patch.object(ips._log, "exception") as mock_log,
        ):
            mock_fire.side_effect = RuntimeError("boom")
            factory = MagicMock()
            await ips._fire_polling_wrapper(factory, info)
        mock_log.assert_called_once()
