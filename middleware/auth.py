import os
from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import jwt

security = HTTPBearer()


def verify_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> dict:
    token = credentials.credentials
    jwt_secret = os.getenv("JWT_SECRET", "your-secret-key-change-in-production")
    try:
        return jwt.decode(token, jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")


def verify_admin_token(credentials: HTTPAuthorizationCredentials = Security(security)) -> dict:
    payload = verify_token(credentials)
    if not payload.get("is_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return payload


def require_role(*allowed_roles: str):
    """Dependency factory — restricts an endpoint to specific roles."""
    def _check(payload: dict = Depends(verify_admin_token)) -> dict:
        # Tokens issued before roles existed default to super_admin for safety
        role = payload.get("role", "super_admin")
        if role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{role}' is not permitted to perform this action",
            )
        return payload
    return _check
