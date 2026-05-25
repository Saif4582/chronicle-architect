from collections import defaultdict
from datetime import datetime, timedelta
from fastapi import Request, Response
from fastapi.responses import JSONResponse
import time

RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX_REQUESTS = 5
RATE_LIMIT_RETRY_AFTER = 900  # seconds (15 minutes)

_rate_limit_store: dict[str, list[float]] = defaultdict(list)


async def rate_limit_middleware(request: Request, call_next):
    # Only apply to login and register paths
    if request.url.path in ("/api/login", "/api/register"):
        ip = request.client.host if request.client else "unknown"
        now = time.time()

        # Clean old entries
        store = _rate_limit_store[ip]
        _rate_limit_store[ip] = [t for t in store if now - t < RATE_LIMIT_WINDOW]

        # Check limit
        if len(_rate_limit_store[ip]) >= RATE_LIMIT_MAX_REQUESTS:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests. Try again in 15 minutes."},
                headers={"Retry-After": str(RATE_LIMIT_RETRY_AFTER)},
            )

        # Record this request
        _rate_limit_store[ip].append(now)

    response = await call_next(request)
    return response
