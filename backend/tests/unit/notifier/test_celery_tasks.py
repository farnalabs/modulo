"""Unit tests for notification Celery task and optional Celery dispatch mode."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from cryptography.fernet import Fernet

from modulo.core.notifier import DispatchResult, Notifier
from modulo.core.notifier.celery_tasks import (
    DispatchNotificationTask,
    enqueue_dispatch,
    get_celery_app,
)

_FERNET_KEY = Fernet.generate_key().decode()
_ORG = uuid.uuid4()
_RUN = uuid.uuid4()
_TEAM = uuid.uuid4()


# ---------------------------------------------------------------------------
# enqueue_dispatch
# ---------------------------------------------------------------------------


async def test_enqueue_dispatch_sends_celery_task() -> None:
    """When Celery is available, enqueue_dispatch sends a task to the broker."""
    send_task = MagicMock()

    with patch("modulo.core.notifier.celery_tasks.get_celery_app") as mock_get_app:
        mock_app = MagicMock()
        mock_app.send_task = send_task
        mock_get_app.return_value = mock_app

        results = await enqueue_dispatch(
            _ORG,
            "hitl_awaiting",
            {"run_id": str(_RUN)},
            run_id=_RUN,
        )

    assert len(results) == 1
    assert results[0]["status"] == "enqueued"
    send_task.assert_called_once()
    name, task_args = send_task.call_args[0][0], send_task.call_args[1].get("args")
    assert name == "modulo.notifier.dispatch"
    assert task_args == [str(_ORG), "hitl_awaiting", json.dumps({"run_id": str(_RUN)}), str(_RUN), False, None]


async def test_enqueue_dispatch_with_team_id() -> None:
    """Team_id is passed through to the Celery task."""
    send_task = MagicMock()

    with patch("modulo.core.notifier.celery_tasks.get_celery_app") as mock_get_app:
        mock_app = MagicMock()
        mock_app.send_task = send_task
        mock_get_app.return_value = mock_app

        await enqueue_dispatch(
            _ORG,
            "hitl_awaiting",
            {"run_id": str(_RUN)},
            run_id=_RUN,
            team_id=_TEAM,
        )

        task_args = send_task.call_args[1].get("args")
        assert task_args[5] == str(_TEAM)


async def test_enqueue_dispatch_falls_back_to_inline_when_celery_unavailable() -> None:
    """When Celery app raises, enqueue_dispatch falls back to inline dispatch."""
    with (
        patch("modulo.core.notifier.celery_tasks.get_celery_app") as mock_get_app,
        patch("modulo.core.notifier.celery_tasks._get_engine") as _mock_get_engine,
        patch("modulo.core.notifier.celery_tasks.Notifier") as mock_notifier_cls,
    ):
        mock_get_app.side_effect = ImportError("Celery not available")
        mock_notifier = MagicMock()
        mock_notifier.dispatch_event = AsyncMock(
            return_value=[
                DispatchResult(
                    endpoint_id=uuid.uuid4(),
                    status="delivered",
                    attempt_count=1,
                    response_code=200,
                )
            ]
        )
        mock_notifier_cls.return_value = mock_notifier

        results = await enqueue_dispatch(
            _ORG,
            "run_failed",
            {"error": "test"},
        )

    assert len(results) == 1
    assert results[0].status == "delivered"
    mock_notifier.dispatch_event.assert_called_once()


# ---------------------------------------------------------------------------
# get_celery_app
# ---------------------------------------------------------------------------


def test_get_celery_app_returns_celery_app() -> None:
    """get_celery_app returns a Celery app instance."""
    from celery import Celery as CeleryCls

    mock_celery = MagicMock(spec=CeleryCls)
    import modulo.core.notifier.celery_tasks as ct

    ct._APP = mock_celery
    try:
        app = get_celery_app()
        assert app is mock_celery
    finally:
        ct._APP = None


# ---------------------------------------------------------------------------
# DispatchNotificationTask
# ---------------------------------------------------------------------------


def test_dispatch_notification_task_autoretry_config() -> None:
    """Task has the correct autoretry configuration."""
    assert DispatchNotificationTask.max_retries == 3
    assert DispatchNotificationTask.default_retry_delay == 5
    assert DispatchNotificationTask.autoretry_for == (Exception,)
    assert DispatchNotificationTask.name == "modulo.notifier.dispatch"


@patch("modulo.core.notifier.celery_tasks._dispatch_notification", new_callable=AsyncMock)
def test_dispatch_notification_task_run(mock_dispatch: AsyncMock) -> None:
    """Task.run() calls _dispatch_notification with parsed parameters."""
    mock_dispatch.return_value = [
        {
            "endpoint_id": str(uuid.uuid4()),
            "status": "delivered",
            "attempt_count": 1,
            "response_code": 200,
            "last_error": None,
        }
    ]

    task = DispatchNotificationTask()
    results = task.run(
        org_id=str(_ORG),
        event_type="hitl_awaiting",
        payload_json=json.dumps({"run_id": str(_RUN)}),
        run_id=str(_RUN),
    )

    assert len(results) == 1
    assert results[0]["status"] == "delivered"
    mock_dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Notifier Celery mode (use_celery=True)
# ---------------------------------------------------------------------------


async def test_notifier_celery_mode_enqueues_instead_of_inline() -> None:
    """When use_celery=True, dispatch_event enqueues to Celery."""
    n = Notifier(MagicMock(), _FERNET_KEY, use_celery=True)

    with patch.object(n, "_dispatch_via_celery", new_callable=AsyncMock) as mock_celery:
        mock_celery.return_value = [
            DispatchResult(
                endpoint_id=_RUN,
                status="enqueued",
                attempt_count=0,
            )
        ]

        results = await n.dispatch_event(_ORG, "hitl_awaiting", {"run_id": str(_RUN)})

    assert len(results) == 1
    assert results[0].status == "enqueued"
    mock_celery.assert_called_once_with(
        _ORG, "hitl_awaiting", {"run_id": str(_RUN)}, run_id=None, retain_payload=False, team_id=None
    )


async def test_notifier_default_mode_still_inline() -> None:
    """When use_celery=False (default), dispatch_event runs inline as before."""
    n = Notifier(MagicMock(), _FERNET_KEY)

    with patch.object(n, "_get_subscribed_endpoints", AsyncMock(return_value=[])) as mock_get:
        results = await n.dispatch_event(_ORG, "hitl_awaiting", {"run_id": str(_RUN)})

    assert results == []
    mock_get.assert_called_once()


async def test_dispatch_via_celery_calls_enqueue_dispatch() -> None:
    """_dispatch_via_celery delegates to enqueue_dispatch and wraps results."""
    n = Notifier(MagicMock(), _FERNET_KEY)

    with patch(
        "modulo.core.notifier.celery_tasks.enqueue_dispatch",
        new_callable=AsyncMock,
    ) as mock_enqueue:
        mock_enqueue.return_value = [
            {
                "endpoint_id": str(uuid.uuid4()),
                "status": "enqueued",
                "attempt_count": 0,
                "response_code": None,
                "last_error": None,
            }
        ]

        results = await n._dispatch_via_celery(
            _ORG,
            "hitl_awaiting",
            {"run_id": str(_RUN)},
        )

    assert len(results) == 1
    assert results[0].status == "enqueued"


async def test_dispatch_via_celery_falls_back_on_exception() -> None:
    """When enqueue_dispatch raises, _dispatch_via_celery falls back to inline."""
    n = Notifier(MagicMock(), _FERNET_KEY)

    with (
        patch("modulo.core.notifier.celery_tasks.enqueue_dispatch", new_callable=AsyncMock) as mock_enqueue,
        patch.object(n, "_get_subscribed_endpoints", AsyncMock(return_value=[])) as mock_get,
    ):
        mock_enqueue.side_effect = RuntimeError("Broker unreachable")

        results = await n._dispatch_via_celery(
            _ORG,
            "hitl_awaiting",
            {"run_id": str(_RUN)},
        )

    assert results == []
    mock_get.assert_called_once()
