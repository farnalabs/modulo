"""Tests for hitl.py _build_resume_executor notifier-init isolation (FAR-882).

Notifying is a BEST-EFFORT side effect of a HITL resume: any failure while
constructing the Notifier (bad fernet key, bad settings, a broken dependency)
must degrade to ``notifier=None`` WITHOUT aborting the resume — the resume
still runs, and the failure surfaces as a log record. Nothing here narrows that
guarantee; these tests pin it so a future "narrowing" sweep cannot silently
convert best-effort notification setup into a resume-blocking 500.
"""

from unittest.mock import MagicMock, patch


class TestBuildResumeExecutorNotifierInit:
    """FAR-882: notifier construction failure NEVER aborts the executor build —
    every init failure degrades to ``notifier=None`` (best-effort side effect)."""

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
    def test_runtime_error_caught_fail_open(self, mock_settings: MagicMock, mock_notifier_cls: MagicMock) -> None:
        """An unexpected RuntimeError (e.g. settings/read failure) from Notifier
        init must ALSO degrade to notifier=None — the resume still runs; only
        the notification side effect is lost (mirrors the pre-existing contract
        in test_hitl_routes_coverage.py)."""
        mock_settings.return_value = MagicMock(
            fernet_key="valid-key-but-not-relevant",
            database_url="postgresql+asyncpg://localhost/test",
        )
        mock_notifier_cls.side_effect = RuntimeError("unexpected")

        from modulo.api.routes.hitl import _build_resume_executor

        executor = _build_resume_executor(MagicMock())
        assert executor is not None
