from __future__ import annotations

from dataclasses import dataclass

import httpx


class WaveError(Exception):
    def __init__(self, message: str, status_code: int = 502, error_code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class WaveConfigError(WaveError):
    def __init__(self, message: str = "Wave payouts are not configured or enabled."):
        super().__init__(message, 503)


class WaveUncertainError(WaveError):
    """The request may have reached Wave; reconcile using the same idempotency key."""


@dataclass
class WaveClientConfig:
    base_url: str
    api_key: str
    business_country: str
    business_currency: str
    aggregated_merchant_id: str = ""
    verify_recipient: bool = True
    enabled: bool = True


def _headers(config: WaveClientConfig, idempotency_key: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"}
    if idempotency_key:
        headers["idempotency-key"] = idempotency_key
    return headers


def _error(response: httpx.Response) -> WaveError:
    try:
        data = response.json()
    except Exception:
        data = {}
    code = data.get("error_code")
    message = data.get("error_message") or data.get("detail") or response.text or "Wave rejected the request"
    return WaveError(message, response.status_code, code)


async def verify_recipient(
    config: WaveClientConfig, *, mobile: str, name: str | None, amount: float, currency: str
) -> dict:
    if not config.enabled or not config.api_key:
        raise WaveConfigError()
    payload = {"mobile": mobile, "amount": str(round(amount, 2)), "currency": currency}
    if name:
        payload["name"] = name
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.post(
                f"{config.base_url}/v1/verify_recipient/",
                headers=_headers(config),
                json=payload,
            )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise WaveError("Wave recipient verification is temporarily unavailable.", 503) from exc
    if response.is_error:
        raise _error(response)
    return response.json()


async def create_payout(
    config: WaveClientConfig,
    *,
    mobile: str,
    name: str,
    amount: float,
    currency: str,
    client_reference: str,
    payment_reason: str = "",
) -> dict:
    if not config.enabled or not config.api_key:
        raise WaveConfigError()
    if currency.upper() != config.business_currency.upper():
        raise WaveError(
            f"Wave business wallet currency is {config.business_currency}; payout currency {currency} is not allowed.",
            400,
            "currency-mismatch",
        )
    payload = {
        "currency": currency.upper(),
        "receive_amount": str(round(amount, 2)),
        "mobile": mobile,
        "name": name[:255],
        "client_reference": client_reference[:255],
    }
    if payment_reason:
        payload["payment_reason"] = payment_reason[:40]
    if config.aggregated_merchant_id:
        payload["aggregated_merchant_id"] = config.aggregated_merchant_id
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{config.base_url}/v1/payout",
                headers=_headers(config, client_reference),
                json=payload,
            )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise WaveUncertainError(
            "Wave payout outcome is unknown; it has been retained for reconciliation.",
            503,
        ) from exc
    if response.status_code >= 500:
        raise WaveUncertainError(
            "Wave returned a server error; payout outcome is retained for reconciliation.",
            503,
        )
    if response.is_error:
        raise _error(response)
    return response.json()


async def get_payout(config: WaveClientConfig, payout_id: str) -> dict:
    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.get(
            f"{config.base_url}/v1/payout/{payout_id}",
            headers=_headers(config),
        )
    if response.is_error:
        raise _error(response)
    return response.json()
