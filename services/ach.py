"""
ACH payment processing via the ACH sandbox API.

Auth flow: exchange an API key for a short-lived Bearer token
(POST /auth/token), then use that token for all payment calls.
Tokens are cached in memory and refreshed automatically before expiry.

ACH Debit  (pull): charge a user's bank account → credit the platform account.
ACH Credit (push): debit the platform account → credit a user's bank account.

Both operations are asynchronous. The sandbox transitions payments through
PENDING → PROCESSING → COMPLETED | FAILED on its own schedule.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx


class ACHError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class ACHConfigError(ACHError):
    """Raised when the ACH config is missing or disabled."""
    def __init__(self, message: str = "ACH not configured. Set it up in the admin portal."):
        super().__init__(message, 503)


@dataclass
class AchClientConfig:
    base_url: str
    api_key: str
    platform_account_number: str
    platform_routing_number: str
    platform_account_type: str    # "CHECKING" | "SAVINGS"
    platform_account_name: str
    enabled: bool = True


@dataclass
class ACHResult:
    payment_id: str        # sandbox paymentId
    trace_number: str
    status: str            # PENDING | PROCESSING | COMPLETED | FAILED


# ---------------------------------------------------------------------------
# Token cache — keyed by api_key, avoids a round-trip on every payment
# ---------------------------------------------------------------------------

_token_cache: dict[str, dict] = {}
_cache_lock = asyncio.Lock()


async def _get_bearer_token(base_url: str, api_key: str) -> str:
    async with _cache_lock:
        cached = _token_cache.get(api_key)
        if cached:
            # Refresh 5 min before expiry
            if datetime.now(timezone.utc) < cached["expires_at"] - timedelta(minutes=5):
                return cached["token"]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{base_url}/auth/token",
                json={"apiKey": api_key, "grantType": "client_credentials"},
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ACHError(f"ACH auth failed: {exc.response.text}", 502) from exc
    except Exception as exc:
        raise ACHError(f"ACH auth unreachable: {exc}", 503) from exc

    data = resp.json()["data"]
    expires_at = datetime.fromisoformat(data["expiresAt"].replace("Z", "+00:00"))

    async with _cache_lock:
        _token_cache[api_key] = {"token": data["accessToken"], "expires_at": expires_at}

    return data["accessToken"]


# ---------------------------------------------------------------------------
# Payment helpers
# ---------------------------------------------------------------------------

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _post_payment(config: AchClientConfig, payload: dict) -> ACHResult:
    token = await _get_bearer_token(config.base_url, config.api_key)
    try:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        ) as client:
            resp = await client.post(f"{config.base_url}/payments/ach", json=payload)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ACHError(f"ACH payment rejected: {exc.response.text}", 502) from exc
    except httpx.TimeoutException as exc:
        raise ACHError("ACH API timed out — payment not confirmed.", 503) from exc
    except Exception as exc:
        raise ACHError(f"ACH API unreachable: {exc}", 503) from exc

    data = resp.json()["data"]
    return ACHResult(
        payment_id=data["paymentId"],
        trace_number=data.get("traceNumber", ""),
        status=data["status"],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def initiate_debit(
    *,
    config: AchClientConfig,
    routing_number: str,
    account_number: str,
    account_type: str,         # "CHECKING" | "SAVINGS"
    account_name: str,
    amount: float,
    reference_id: str,
    description: str = "",
) -> ACHResult:
    """
    Pull funds FROM a user's bank account INTO the platform account (top-up).

    The user's account is the debit side; the platform account is the credit side.
    """
    if not config.enabled:
        raise ACHConfigError()

    return await _post_payment(config, {
        "type": "DEBIT",
        "amount": round(amount, 2),
        "currency": "USD",
        "effectiveDate": _today(),
        "entryClassCode": "PPD",
        "description": description[:80] or "Wallet top-up",
        "referenceId": reference_id[:64],
        "debitAccount": {
            "accountNumber": account_number,
            "routingNumber": routing_number,
            "accountType": account_type.upper(),
            "accountName": account_name[:22],
        },
        "creditAccount": {
            "accountNumber": config.platform_account_number,
            "routingNumber": config.platform_routing_number,
            "accountType": config.platform_account_type,
            "accountName": config.platform_account_name[:22],
        },
    })


async def initiate_credit(
    *,
    config: AchClientConfig,
    routing_number: str,
    account_number: str,
    account_type: str,
    account_name: str,
    amount: float,
    reference_id: str,
    description: str = "",
) -> ACHResult:
    """
    Push funds FROM the platform account TO a user's bank account (payout).

    The platform account is the debit side; the user's account is the credit side.
    """
    if not config.enabled:
        raise ACHConfigError()

    return await _post_payment(config, {
        "type": "CREDIT",
        "amount": round(amount, 2),
        "currency": "USD",
        "effectiveDate": _today(),
        "entryClassCode": "PPD",
        "description": description[:80] or "Wallet payout",
        "referenceId": reference_id[:64],
        "debitAccount": {
            "accountNumber": config.platform_account_number,
            "routingNumber": config.platform_routing_number,
            "accountType": config.platform_account_type,
            "accountName": config.platform_account_name[:22],
        },
        "creditAccount": {
            "accountNumber": account_number,
            "routingNumber": routing_number,
            "accountType": account_type.upper(),
            "accountName": account_name[:22],
        },
    })


async def get_payment(*, config: AchClientConfig, payment_id: str) -> dict:
    """Fetch the current status of a payment from the sandbox."""
    token = await _get_bearer_token(config.base_url, config.api_key)
    try:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        ) as client:
            resp = await client.get(f"{config.base_url}/payments/ach/{payment_id}")
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ACHError(f"ACH get payment failed: {exc.response.text}", exc.response.status_code) from exc
    except Exception as exc:
        raise ACHError(f"ACH API unreachable: {exc}", 503) from exc

    return resp.json()["data"]
