"""Unit tests for DatabaseCronEntry — Celery beat entry lifecycle."""

import datetime
import uuid

from modulo.core.cron_scheduler import DatabaseCronEntry


class TestDatabaseCronEntry:
    def test_entry_properties(self):
        now = datetime.datetime.now(datetime.UTC)
        entry = DatabaseCronEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            snapshot_id=uuid.uuid4(),
            cron_expression="*/5 * * * *",
            next_fire_at=now,
        )
        assert entry.name.startswith("cron-")
        assert entry.task == "modulo.cron.fire_trigger"
        assert len(entry.args) == 5
        assert entry.args[4] == "*/5 * * * *"
        assert isinstance(entry.schedule, DatabaseCronEntry)

    def test_is_due_when_past(self):
        past = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)
        entry = DatabaseCronEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            snapshot_id=uuid.uuid4(),
            cron_expression="* * * * *",
            next_fire_at=past,
        )
        due, delay = entry.is_due()
        assert due is True
        assert delay.total_seconds() == 0

    def test_is_not_due_when_future(self):
        future = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=1)
        entry = DatabaseCronEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            snapshot_id=uuid.uuid4(),
            cron_expression="0 * * * *",
            next_fire_at=future,
        )
        due, delay = entry.is_due()
        assert due is False
        assert delay.total_seconds() > 0

    def test_is_due_when_exactly_now(self):
        now = datetime.datetime.now(datetime.UTC)
        entry = DatabaseCronEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            snapshot_id=uuid.uuid4(),
            cron_expression="* * * * *",
            next_fire_at=now,
        )
        due, delay = entry.is_due()
        assert due is True

    def test_repr(self):
        now = datetime.datetime.now(datetime.UTC)
        entry = DatabaseCronEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            snapshot_id=uuid.uuid4(),
            cron_expression="0 9 * * *",
            next_fire_at=now,
        )
        r = repr(entry)
        assert "DatabaseCronEntry" in r
        assert "next=" in r

    def test_options_contains_unique_task_id(self):
        now = datetime.datetime.now(datetime.UTC)
        entry = DatabaseCronEntry(
            trigger_id=uuid.uuid4(),
            org_id=uuid.uuid4(),
            pipeline_id=uuid.uuid4(),
            snapshot_id=uuid.uuid4(),
            cron_expression="* * * * *",
            next_fire_at=now,
        )
        opts = entry.options
        assert "task_id" in opts
        assert opts["task_id"].startswith("cron-")
