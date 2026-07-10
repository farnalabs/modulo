"""Test that celery_app module handles missing Celery gracefully."""

import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def clear_celery_module_cache():
    """Remove modulo.celery_app from sys.modules so it gets re-imported."""
    keys = [k for k in sys.modules if k.startswith("modulo.celery_app")]
    for k in keys:
        del sys.modules[k]
    yield


class TestGetCeleryApp:
    def test_returns_none_when_no_redis_url(self, clear_celery_module_cache):
        """get_celery_app() should return None when redis_url is not configured."""
        with patch("modulo.settings.get_settings") as mock_settings:
            settings = MagicMock()
            settings.redis_url = ""
            mock_settings.return_value = settings

            import modulo.celery_app

            importlib.reload(modulo.celery_app)
            result = modulo.celery_app.get_celery_app()
            assert result is None

    def test_returns_instance_with_redis(self, clear_celery_module_cache):
        """get_celery_app() should return a Celery instance when redis_url is set."""
        with patch("modulo.settings.get_settings") as mock_settings:
            settings = MagicMock()
            settings.redis_url = "redis://localhost:6379/0"
            settings.fernet_key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            mock_settings.return_value = settings

            import modulo.celery_app

            importlib.reload(modulo.celery_app)
            result = modulo.celery_app.get_celery_app()
            assert result is not None
            assert result.conf.broker_url == "redis://localhost:6379/0"

    def test_module_has_celery_sentinel(self):
        """When Celery IS installed, Celery class is the real Celery class."""
        import modulo.celery_app

        assert modulo.celery_app.Celery is not None

    def test_module_has_get_celery_app_function(self):
        """The module exports get_celery_app function."""
        import modulo.celery_app

        assert hasattr(modulo.celery_app, "get_celery_app")
        assert callable(modulo.celery_app.get_celery_app)

    def test_celery_app_attr_is_none_initially(self):
        """The module-level celery_app attribute is None initially."""
        import modulo.celery_app

        importlib.reload(modulo.celery_app)
        assert modulo.celery_app.celery_app is None


class TestCronSchedulerSentinel:
    def test_module_loads(self):
        """cron_scheduler module should load without errors."""
        import modulo.core.cron_scheduler

        importlib.reload(modulo.core.cron_scheduler)
        assert modulo.core.cron_scheduler.CronFireTask is not None

    def test_has_celery_fallbacks(self):
        """Object sentinel fallbacks exist for Celery classes."""
        import modulo.core.cron_scheduler

        importlib.reload(modulo.core.cron_scheduler)
        assert modulo.core.cron_scheduler.Celery is not None
        assert modulo.core.cron_scheduler.Task is not None
        assert modulo.core.cron_scheduler.ScheduleEntry is not None
        assert modulo.core.cron_scheduler.Scheduler is not None


class TestCeleryTasksGuard:
    def test_module_loads(self):
        """celery_tasks module should load without errors."""
        import modulo.core.notifier.celery_tasks as ct

        assert ct.get_celery_app is not None

    def test_has_celery_fallbacks(self):
        """Module has Celery and Task attributes."""
        import modulo.core.notifier.celery_tasks as ct

        assert hasattr(ct, "Celery")
        assert hasattr(ct, "Task")


class TestCleanupJobGuard:
    def test_module_loads(self):
        """webhook_dedup_cleanup module should load without errors."""
        import modulo.core.cleanup_jobs.webhook_dedup_cleanup as wc

        assert wc.WebhookDedupCleanupTask is not None


class TestReportSchedulerGuard:
    def test_module_loads(self):
        """reports/scheduler module should load without errors."""
        import modulo.core.reports.scheduler as rs

        assert rs.get_celery_app is not None
