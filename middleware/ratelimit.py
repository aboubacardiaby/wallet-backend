import time
import threading
from fastapi import HTTPException, Request, status

_visitors: dict = {}
_lock = threading.Lock()


def _cleanup_visitors():
    now = time.time()
    with _lock:
        stale = [ip for ip, v in _visitors.items() if now - v["last_seen"] > 180]
        for ip in stale:
            del _visitors[ip]


async def rate_limiter(request: Request):
    ip = request.client.host
    now = time.time()

    with _lock:
        visitor = _visitors.get(ip)

        if visitor is None:
            _visitors[ip] = {"last_seen": now, "count": 1}
            return

        # Reset count if more than 1 minute has passed
        if now - visitor["last_seen"] > 60:
            visitor["count"] = 1
            visitor["last_seen"] = now
            return

        if visitor["count"] > 100:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Try again later.",
            )

        visitor["count"] += 1
        visitor["last_seen"] = now

    # Periodic cleanup (every ~50 requests, non-blocking)
    if int(now) % 180 == 0:
        threading.Thread(target=_cleanup_visitors, daemon=True).start()
