"""Worker-side Redis event subscription (FAR-250).

SAQ workers connect and subscribe to the org resource channels but NEVER
relay: only the web process runs the :mod:`modulo.core.events.relay` pump
that re-injects messages into an in-memory EventBus (only the web process
serves SSE). The worker subscription is a persistent, consume-and-drop
subscription that verifies the pub/sub leg at boot and re-attempts with
capped exponential backoff until it succeeds — a Redis blip at boot must
never crash the worker (``policy="always"`` crash-loop risk).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modulo.core.events.redis_broker import RedisEventBroker

_log = logging.getLogger(__name__)

# Subscribe to the same channel namespace the EventBus broadcasts on; every
# message is consumed and dropped (workers never relay).
WORKER_CHANNEL_PATTERN = "resource:*"

_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 60.0


async def run_worker_event_subscription(broker: RedisEventBroker) -> None:
    """Hold a pattern subscription over ``resource:*``, dropping all messages.

    Retries connect+subscribe with capped exponential backoff until it
    succeeds ("re-attempt subscribe until it succeeds"); after a successful
    subscription drops any later errors and re-subscribes the same way.
    Cancellation always propagates.
    """
    backoff = _BACKOFF_BASE_SECONDS
    while True:
        pubsub = None
        try:
            await broker.connect()
            pubsub = await broker.subscribe_pattern(WORKER_CHANNEL_PATTERN)
            _log.info("worker_event_subscription.subscribed", extra={"pattern": WORKER_CHANNEL_PATTERN})
            backoff = _BACKOFF_BASE_SECONDS
            async for _message in pubsub.listen():
                # Consume and drop — workers subscribe but never relay.
                continue
            _log.warning("worker_event_subscription.listen_ended", extra={"pattern": WORKER_CHANNEL_PATTERN})
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.warning(
                "worker_event_subscription.failed",
                extra={"pattern": WORKER_CHANNEL_PATTERN, "backoff_s": backoff},
                exc_info=True,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
        finally:
            await _close_pubsub(pubsub)


async def _close_pubsub(pubsub: object | None) -> None:
    if pubsub is None:
        return
    try:
        unsubscribe = getattr(pubsub, "unsubscribe", None)
        if unsubscribe is not None:
            await unsubscribe()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.debug("worker_event_subscription.unsubscribe_failed", exc_info=True)
    try:
        aclose = getattr(pubsub, "aclose", None)
        if aclose is not None:
            await aclose()
        else:
            close = getattr(pubsub, "close", None)  # redis<5 compatibility
            if close is not None:
                await close()
    except asyncio.CancelledError:
        raise
    except Exception:
        _log.debug("worker_event_subscription.close_failed", exc_info=True)


def spawn_worker_event_subscription(broker: RedisEventBroker) -> asyncio.Task[None] | None:
    """Spawn the subscription loop as a fire-and-forget background task.

    Returns ``None`` (fail-open, with a log) when no event loop is running —
    the async startup hook always has one. The guarded
    ``loop.create_task`` idiom (same as
    ``EventBus._redis_broadcast_if_configured``) cannot raise the
    "no running event loop" error the
    semgrep.create-task-without-guard rule exists to prevent.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _log.warning("worker_event_subscription.spawn_skipped_no_loop")
        return None
    task = loop.create_task(run_worker_event_subscription(broker))
    _held_tasks.add(task)
    task.add_done_callback(_on_task_done)
    return task


def _on_task_done(task: asyncio.Task[None]) -> None:
    _held_tasks.discard(task)
    if task.cancelled():
        return
    with contextlib.suppress(asyncio.CancelledError):
        exc = task.exception()
    if exc is not None:
        _log.warning("worker_event_subscription.task_failed", exc_info=exc)


_held_tasks: set[asyncio.Task[None]] = set()
