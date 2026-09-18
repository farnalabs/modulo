import asyncio


async def unsafe_handle_wait() -> None:
    # ruleid: asyncio-waitfor-wait-unshielded
    await asyncio.wait_for(handle.wait(), timeout=5)


async def unsafe_sdk_wait() -> None:
    # ruleid: asyncio-waitfor-wait-unshielded
    result = await asyncio.wait_for(self._sdk.thing.wait(), 30)


async def unsafe_no_timeout() -> None:
    # ruleid: asyncio-waitfor-wait-unshielded
    await asyncio.wait_for(proc_handle.wait())


def safe_shielded() -> None:
    # ok: asyncio-waitfor-wait-unshielded
    asyncio.wait_for(asyncio.shield(handle.wait()), timeout=5)


async def safe_non_wait_call() -> None:
    # ok: asyncio-waitfor-wait-unshielded
    await asyncio.wait_for(sandbox.create(), 30)


async def safe_event_wait() -> None:
    # ok: asyncio-waitfor-wait-unshielded
    await asyncio.wait_for(event.wait(), timeout=10)


async def safe_proc_wait() -> None:
    # ok: asyncio-waitfor-wait-unshielded
    await asyncio.wait_for(proc.wait(), timeout=_GRACE_KILL_TIMEOUT)


async def safe_first_progress_wait() -> None:
    # ok: asyncio-waitfor-wait-unshielded
    await asyncio.wait_for(first_progress.wait(), timeout=grace_seconds)


async def safe_resume_ev_wait() -> None:
    # ok: asyncio-waitfor-wait-unshielded
    await asyncio.wait_for(resume_ev.wait(), timeout=300.0)
