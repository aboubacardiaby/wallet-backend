import asyncio
from datetime import datetime, timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import _SessionLocal
from models.rate_limit import RateLimit

_LIMIT = 100            # requests per window
_WINDOW = 60            # seconds
_CLEANUP_EVERY = 100    # approximate requests between DB cleanups
_request_count = 0
_lock = asyncio.Lock()


async def _cleanup_old(db: AsyncSession) -> None:
    """Remove rate limit rows that are outside the active window."""
    cutoff = datetime.utcnow() - timedelta(seconds=_WINDOW)
    await db.execute(delete(RateLimit).where(RateLimit.window_start < cutoff))
    await db.commit()


async def rate_limiter(request: Request):
    ip = request.client.host
    now = datetime.utcnow()
    window_start = now.replace(second=0, microsecond=0)

    if _SessionLocal is None:
        # DB is not ready; allow the request through.
        return

    async with _SessionLocal() as db:
        row = await db.scalar(select(RateLimit).where(RateLimit.ip_address == ip))

        if not row or row.window_start < window_start:
            # New window for this IP.
            if row:
                row.count = 1
                row.window_start = window_start
                row.last_seen = now
            else:
                db.add(RateLimit(ip_address=ip, count=1, window_start=window_start, last_seen=now))
            await db.commit()
            return

        if row.count > _LIMIT:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Try again later.",
            )

        row.count += 1
        row.last_seen = now
        await db.commit()

    # Periodic cleanup of expired windows.
    global _request_count
    async with _lock:
        _request_count += 1
        if _request_count % _CLEANUP_EVERY == 0:
            asyncio.create_task(_cleanup_expired_windows())


async def _cleanup_expired_windows() -> None:
    if _SessionLocal is None:
        return
    async with _SessionLocal() as db:
        await _cleanup_old(db)
