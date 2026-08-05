"""Unit tests for the E2B dispatch idempotency fence (plan F3a).

Covers the SETNX-before-dispatch key lifecycle against a fake in-memory Redis
client (no live infra): per-run keying, same-claim dedup (exactly one sandbox),
superseded-claim refusal, fenced release on dispatch failure, terminal release
via mark_complete, and the ~8h TTL bound.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import modulo.core.pipeline_execution as pe


class _FakeRedis:
    """Minimal in-memory Redis double: set(nx=True), get, delete."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def delete(self, key: str) -> int:
        return 1 if self.data.pop(key, None) is not None else 0

    async def aclose(self) -> None:
        return None


def _fence() -> tuple[_FakeRedis, pe]:
    fake = _FakeRedis()
    patcher = patch.object(pe, "_e2b_client", AsyncMock(return_value=fake))
    return fake, patcher


@pytest.mark.asyncio
async def test_acquire_uses_per_run_key_with_token_value() -> None:
    fake, patcher = _fence()
    with patcher:
        await pe.e2b_dispatch_acquire("run-1", "tok-a")

    assert fake.data == {"run:run-1:e2b": "tok-a"}


@pytest.mark.asyncio
async def test_acquire_sets_8h_ttl() -> None:
    _, patcher = _fence()
    with patcher, patch.object(pe, "_e2b_client") as mock_getter:
        redis = MagicMock()
        redis.set = AsyncMock(return_value=True)
        redis.aclose = AsyncMock()
        mock_getter.return_value = redis
        await pe.e2b_dispatch_acquire("run-1", "tok-a")

    redis.set.assert_awaited_once_with("run:run-1:e2b", "tok-a", nx=True, ex=pe.E2B_IDEMPOTENCY_TTL_SECONDS)
    redis.aclose.assert_awaited_once()
    assert pe.E2B_IDEMPOTENCY_TTL_SECONDS == 8 * 3600


@pytest.mark.asyncio
async def test_acquire_refuses_superseded_claim() -> None:
    """Key already exists with a DIFFERENT token -> a superseded claim aborts."""
    fake, patcher = _fence()
    with patcher:
        await pe.e2b_dispatch_acquire("run-1", "tok-a")  # original wins
        with pytest.raises(pe.E2BIdempotencyDeniedError):
            await pe.e2b_dispatch_acquire("run-1", "tok-b")  # successor refused

    assert fake.data == {"run:run-1:e2b": "tok-a"}


@pytest.mark.asyncio
async def test_acquire_dedups_transient_retry_within_same_claim() -> None:
    """Same token on a live dispatch -> exactly one sandbox, the retry aborts."""
    fake, patcher = _fence()
    with patcher:
        await pe.e2b_dispatch_acquire("run-1", "tok-a")
        with pytest.raises(pe.E2BIdempotencyDeniedError, match="same-claim"):
            await pe.e2b_dispatch_acquire("run-1", "tok-a")

    assert fake.data == {"run:run-1:e2b": "tok-a"}


@pytest.mark.asyncio
async def test_dispatch_failure_fenced_release_allows_redispatch() -> None:
    """A failing dispatch fenced-releases; the successor's claim can re-dispatch."""
    fake, patcher = _fence()
    with patcher:
        await pe.e2b_dispatch_acquire("run-1", "tok-a")
        await pe.e2b_dispatch_release_fenced("run-1", "tok-a")
        assert fake.data == {}

        # A retry (same or successor claim) can now re-dispatch.
        await pe.e2b_dispatch_acquire("run-1", "tok-b")
        assert fake.data == {"run:run-1:e2b": "tok-b"}


@pytest.mark.asyncio
async def test_fenced_release_never_deletes_successors_key() -> None:
    """A superseded original must not DEL the successor's live dispatch."""
    fake, patcher = _fence()
    with patcher:
        await pe.e2b_dispatch_acquire("run-1", "tok-b")  # successor owns the key
        await pe.e2b_dispatch_release_fenced("run-1", "tok-a")  # original tries to release
        assert fake.data == {"run:run-1:e2b": "tok-b"}


@pytest.mark.asyncio
async def test_terminal_release_deletes_key() -> None:
    fake, patcher = _fence()
    with patcher:
        await pe.e2b_dispatch_acquire("run-1", "tok-a")
        await pe.e2b_dispatch_release_terminal("run-1")
        assert fake.data == {}


@pytest.mark.asyncio
async def test_mark_complete_releases_terminal_key() -> None:
    """The run-level key is DEL'd when the run is marked complete (plan F3a)."""
    run = SimpleNamespace(status="running", completed_at=None, claim_token="tok-a")
    session = MagicMock()
    session.begin.return_value.__aenter__ = AsyncMock(return_value=session)
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(pe, "get_run", AsyncMock(return_value=run)),
        patch.object(pe, "async_sessionmaker", return_value=factory),
        patch.object(pe, "set_rls_org", AsyncMock()),
        patch.object(pe, "e2b_idempotency_enabled", return_value=True),
        patch.object(pe, "e2b_dispatch_release_terminal", AsyncMock()) as release,
    ):
        await pe.mark_complete(MagicMock(), str(uuid.uuid4()), str(uuid.uuid4()))  # type: ignore[arg-type]

    assert run.status == "complete"
    release.assert_awaited_once()
