import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

load_dotenv()

from config.database import connect_db, database_ready, disconnect_db
from config.runtime import cors_origins, is_production, jwt_secret
from handlers import admin, auth, cash, country, exchange, kyc, notification, payment, qr, recipient, transfer, user, wallet
from middleware.ratelimit import rate_limiter


@asynccontextmanager
async def lifespan(app: FastAPI):
    jwt_secret()
    if is_production() and not cors_origins():
        import logging
        logger = logging.getLogger("kalipeh")
        logger.warning("CORS_ORIGINS is not configured in production; CORS will be disabled")
    await connect_db()
    # Pre-load rate overrides and fee rules into memory
    from config.database import _SessionLocal
    from config.rate_config import load_from_db
    if _SessionLocal:
        async with _SessionLocal() as db:
            try:
                await load_from_db(db)
            except Exception as e:
                print(f"WARNING: Could not load rate/fee config: {e}")
    yield
    await disconnect_db()


app = FastAPI(title="Kalipeh Wallet API", version="1.0.0", lifespan=lifespan)

allowed_origins = cors_origins()

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=bool(allowed_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def apply_rate_limit(request: Request, call_next):
    try:
        await rate_limiter(request)
    except Exception as exc:
        return JSONResponse(status_code=429, content={"detail": str(exc)})
    return await call_next(request)


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/ready")
async def ready():
    if not await database_ready():
        return JSONResponse(status_code=503, content={"status": "not_ready", "database": "unavailable"})
    return {"status": "ready", "database": "available"}


prefix = "/api/v1"
app.include_router(auth.router, prefix=prefix)
app.include_router(user.router, prefix=prefix)
app.include_router(wallet.router, prefix=prefix)
app.include_router(transfer.router, prefix=prefix)
app.include_router(cash.router, prefix=prefix)
app.include_router(qr.router, prefix=prefix)
app.include_router(notification.router, prefix=prefix)
app.include_router(recipient.router, prefix=prefix)
app.include_router(kyc.router, prefix=prefix)
app.include_router(exchange.router, prefix=prefix)
app.include_router(country.router, prefix=prefix)
app.include_router(payment.router, prefix=prefix)
app.include_router(admin.router, prefix=prefix)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", 8080))
    print(f"Server starting on port {port}")
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
