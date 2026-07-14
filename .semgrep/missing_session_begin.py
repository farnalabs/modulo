from typing import Any


class Router:
    def get(self, path: str) -> Any: ...


router = Router()


# ruleid: missing-session-begin
@router.get("/unsafe")
async def unsafe_route(session: Any) -> None:
    await session.execute("SELECT 1")
    session.add(object())
    await session.flush()


# ok: missing-session-begin
@router.get("/safe")
async def safe_route(session: Any) -> None:
    async with session.begin():
        await session.execute("SELECT 1")
        session.add(object())
        await session.flush()


# ok: missing-session-begin
@router.get("/safe-db-session")
async def safe_db_session_route(db_session: Any) -> None:
    async with db_session.begin():
        await db_session.execute("SELECT 1")
