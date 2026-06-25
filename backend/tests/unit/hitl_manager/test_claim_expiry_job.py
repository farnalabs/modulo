"""Unit tests for ClaimExpiryJob."""

from unittest.mock import AsyncMock, MagicMock, patch

from modulo.core.hitl_manager.expiry_job import ClaimExpiryJob


async def test_expire_once_resets_stale_claims() -> None:
    engine = MagicMock()
    job = ClaimExpiryJob(engine)

    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    update_result = MagicMock()
    update_result.all.return_value = [
        ("run-1", "gate-a", "org-1"),
        ("run-2", "gate-b", "org-1"),
    ]
    session.execute = AsyncMock(return_value=update_result)

    factory = MagicMock(side_effect=lambda: AsyncMock(
        __aenter__=AsyncMock(return_value=session),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch.object(job, "_session_factory", factory):
        expired = await job._expire_once()

    assert len(expired) == 2
    assert expired[0]["run_id"] == "run-1"
    assert expired[0]["gate_id"] == "gate-a"


async def test_expire_once_empty_when_none_stale() -> None:
    engine = MagicMock()
    job = ClaimExpiryJob(engine)

    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)

    update_result = MagicMock()
    update_result.all.return_value = []
    session.execute = AsyncMock(return_value=update_result)

    factory = MagicMock(side_effect=lambda: AsyncMock(
        __aenter__=AsyncMock(return_value=session),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch.object(job, "_session_factory", factory):
        expired = await job._expire_once()

    assert expired == []


async def test_start_and_stop_lifecycle() -> None:
    engine = MagicMock()
    job = ClaimExpiryJob(engine)

    await job.start()
    assert job._task is not None
    assert not job._task.done()

    await job.stop()
    assert job._task is None
