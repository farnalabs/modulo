"""Shared test doubles for the ``cron_helpers`` test package.

These primitives were byte-identical copies across several modules in this
package; they live here once as the SHARED primitives, which removes the
DUPLICATED copies. This does not fully decouple the test modules:
``test_fire_due_triggers_ongoing_scan`` still imports ``_cron_row``,
``_MockSession``, ``_pause_result``, ``_polling_row`` and ``_report_row`` from
``test_cron_helpers``.

This is a helper module, not a test module — it deliberately contains no tests.
File-specific doubles stay local: the two ``_MockSession`` classes differ (the
``test_cron_helpers`` one pops a fixed result sequence, the dispatcher one
routes by SQL shape), and ``test_dispatcher_reconcile`` needs a richer
``_settings`` than the four-field base here.
"""

from __future__ import annotations

import uuid
from typing import Any, Self
from unittest.mock import MagicMock

import pytest


class _Begin:
    """Async context-manager double: enters itself and never suppresses."""

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


def _patch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv("FERNET_KEY", "b" * 44)


def _mock_result(**kwargs: Any) -> MagicMock:
    result = MagicMock()
    for name, value in kwargs.items():
        getattr(result, name).return_value = value
    return result


def _org_result(org_ids: list[uuid.UUID]) -> MagicMock:
    r = MagicMock()
    r.scalars.return_value = org_ids
    return r


def _rows_result(rows: list[Any]) -> MagicMock:
    r = MagicMock()
    r.all.return_value = rows
    return r


def _settings(**overrides: object) -> MagicMock:
    base: dict[str, object] = {
        "saq_runs_queue": "runs",
        "redis_url": "redis://localhost:6379/0",
        "saq_redis_pool_size": 5,
        "fernet_key": "b" * 44,
    }
    base.update(overrides)
    return MagicMock(**base)
