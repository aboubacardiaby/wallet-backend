import os


_INSECURE_JWT_SECRETS = {"", "your-secret-key-change-in-production", "change-me"}


def is_production() -> bool:
    return os.getenv("APP_ENV", "development").strip().lower() in {"prod", "production"}


def jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET", "")
    if not secret:
        if is_production():
            raise RuntimeError("JWT_SECRET must be configured in production")
        secret = "development-only-secret-change-me"
    if is_production() and secret in _INSECURE_JWT_SECRETS:
        raise RuntimeError("JWT_SECRET is using an insecure production value")
    return secret


def allow_simulated_funding() -> bool:
    return (
        not is_production()
        and os.getenv("ALLOW_SIMULATED_FUNDING", "false").strip().lower() == "true"
    )


def cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "")
    origins = [item.strip() for item in raw.split(",") if item.strip()]
    if origins:
        return origins
    return [] if is_production() else ["http://localhost:3000", "http://localhost:5173"]
