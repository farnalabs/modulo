"""Tests for hitl.py _build_resume_executor narrowed exception handler (FAR-882).

Verifies that:
1. ValueError/TypeError/AttributeError from Notifier init is caught (fail-open).
2. Unexpected exception types propagate (fail-closed for programming errors).
"""

from unittest.mock import MagicMock, patch

import pytest


class TestBuildResumeExecutorNotifierInit:
    """FAR-882: _build_resume_executor catches config-related errors from
    Notifier init but lets unexpected errors propagate."""

    @patch("modulo.api.routes.hitl.Notifier")
    @patch("modulo.api.routes.hitl.get_settings")
    def test_value_error_caught_fail_open(self, mock_settings: MagicMock, mock_notifier_cls: MagicMock) -> None:
        """A bad fernet key raises ValueError — notifier is None but executor
        still builds (fail-open for notification side-effect)."""
        mock_settings.return_value = MagicMock(
            fernet_key="bad-key",
            database_url="postgresql+asyncpg://localhost/test",
        )
        mock_notifier_cls.side_effect = ValueError("Invalid Fernet key")

        from modulo.api.routes.hitl import _build_resume_executor

        executor = _build_resume_executor(MagicMock())
        assert executor is not None

    @patch("modulo.api.routes.hitl.Notifier")
    @patch("modulo.api.routes.hitl.get_settings")
    def test_type_error_caught_fail_open(self, mock_settings: MagicMock, mock_notifier_cls: MagicMock) -> None:
        """TypeError from Notifier init is caught (fail-open)."""
        mock_settings.return_value = MagicMock(
            fernet_key=123,
            database_url="postgresql+asyncpg://localhost/test",
        )
        mock_notifier_cls.side_effect = TypeError("bad key type")

        from modulo.api.routes.hitl import _build_resume_executor

        executor = _build_resume_executor(MagicMock())
        assert executor is not None

    @patch("modulo.api.routes.hitl.Notifier")
    @patch("modulo.api.routes.hitl.get_settings")
    def test_runtime_error_propagates(self, mock_settings: MagicMock, mock_notifier_cls: MagicMock) -> None:
        """An unexpected RuntimeError from Notifier init must propagate —
        programming errors surface as 500, not silently disabled."""
        mock_settings.return_value = MagicMock(
            fernet_key="valid-key-but-not-relevant",
            database_url="postgresql+asyncpg://localhost/test",
        )
        mock_notifier_cls.side_effect = RuntimeError("unexpected")

        from modulo.api.routes.hitl import _build_resume_executor

        with pytest.raises(RuntimeError, match="unexpected"):
            _build_resume_executor(MagicMock())
