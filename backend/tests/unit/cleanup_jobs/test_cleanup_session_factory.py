"""Unit tests for ``modulo.core.saq_worker._cleanup_session_factory``.

The factory picker is shared by every cleanup cron (``webhook_dedup_cleanup``,
``trigger_events_cleanup``, ``expired_webhook_dedup_purge``), so it is tested
once here rather than duplicated into each cron's own test module.
"""

from unittest.mock import MagicMock, patch

import pytest

import modulo.core.saq_worker as sw


class TestCleanupSessionFactory:
    """``_cleanup_session_factory`` picks the session factory by dialect.

    PostgreSQL (RLS, modulo_app NOBYPASSRLS) → the system session factory,
    still failing LOUD when ``MODULO_SYSTEM_DATABASE_URL`` is unset.
    Non-PostgreSQL (SQLite/MariaDB/MySQL — no RLS, no system role) → the
    plain factory; the system engine is never touched.
    """

    def test_postgres_uses_system_factory(self) -> None:
        sentinel = MagicMock(name="system_factory")

        with (
            patch.object(sw, "get_settings", return_value=MagicMock(modulo_db="postgres")),
            patch.object(sw, "_make_system_session_factory", return_value=sentinel) as mock_system,
        ):
            factory = sw._cleanup_session_factory()

        mock_system.assert_called_once()
        assert factory is sentinel

    @pytest.mark.parametrize("non_pg_db", ["sqlite", "mariadb", "mysql", "SQLite", "MariaDB"])
    def test_non_postgres_uses_plain_factory(self, non_pg_db: str) -> None:
        sentinel = MagicMock(name="plain_factory")

        with (
            patch.object(sw, "get_settings", return_value=MagicMock(modulo_db=non_pg_db)),
            patch.object(sw, "_make_session_factory", return_value=sentinel) as mock_plain,
            patch.object(sw, "_make_system_session_factory") as mock_system,
        ):
            factory = sw._cleanup_session_factory()

        mock_plain.assert_called_once()
        mock_system.assert_not_called()
        assert factory is sentinel

    def test_postgres_with_unset_system_url_fails_loud(self) -> None:
        """The PG fail-closed path is preserved: an unset
        ``MODULO_SYSTEM_DATABASE_URL`` raises RuntimeError instead of silently
        running the cross-org purge as modulo_app."""
        settings = MagicMock(modulo_db="postgres")
        settings.modulo_system_database_url = ""

        with (
            patch.object(sw, "get_settings", return_value=settings),
            patch.object(sw, "_SYSTEM_ASYNC_ENGINE", None),
            patch.object(sw, "_make_session_factory") as mock_plain,
            pytest.raises(RuntimeError, match="MODULO_SYSTEM_DATABASE_URL"),
        ):
            sw._cleanup_session_factory()

        mock_plain.assert_not_called()
