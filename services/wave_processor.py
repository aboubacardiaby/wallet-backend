"""
Wave Mobile Money B2C payment processor.

Wraps the Wave API (https://api.wave.com/v1/b2c/payment).
When WAVE_API_KEY is not set the processor runs in simulation mode —
all payments are accepted and a synthetic wave_ref is returned.
"""
import os
from dataclasses import dataclass

import httpx

WAVE_API_BASE = "https://api.wave.com/v1"


@dataclass
class WavePaymentResult:
    wave_ref: str
    simulated: bool


class WavePaymentError(Exception):
    """Raised when Wave rejects or cannot process the payment."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


async def send_b2c_payment(
    *,
    to_phone: str,
    amount: float,
    currency: str,
    transaction_ref: str,
    description: str = "",
) -> WavePaymentResult:
    """
    Disburse funds to a Wave mobile wallet.

    Args:
        to_phone:        Recipient's Wave-registered phone number (E.164).
        amount:          Amount to credit to the recipient (after fee deduction).
        currency:        Destination currency code (e.g. "XOF", "GMD").
        transaction_ref: Internal transaction reference — used as idempotency key.
        description:     Optional transfer note shown to the recipient.

    Returns:
        WavePaymentResult with the Wave transaction ID and a simulated flag.

    Raises:
        WavePaymentError: if Wave rejects the payment or is unreachable.
    """
    wave_api_key = os.getenv("WAVE_API_KEY", "")

    if not wave_api_key:
        # ── Simulation mode (no API key configured) ──────────────────────────
        sim_ref = f"WAVE_SIM_{transaction_ref[:12].upper()}"
        return WavePaymentResult(wave_ref=sim_ref, simulated=True)

    # ── Live Wave B2C API ─────────────────────────────────────────────────────
    try:
        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {wave_api_key}",
                "Content-Type": "application/json",
            },
            timeout=15,
        ) as client:
            resp = await client.post(
                f"{WAVE_API_BASE}/b2c/payment",
                json={
                    "currency":       currency,
                    "receive_amount": str(amount),
                    "mobile":         to_phone,
                    "name":           description or f"Transfer to {to_phone}",
                    "client_reference": transaction_ref,   # idempotency key
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return WavePaymentResult(wave_ref=data["id"], simulated=False)

    except httpx.HTTPStatusError as exc:
        raise WavePaymentError(
            f"Wave payment rejected: {exc.response.text}",
            status_code=502,
        ) from exc
    except httpx.TimeoutException as exc:
        raise WavePaymentError(
            "Wave API timed out — payment not confirmed.",
            status_code=503,
        ) from exc
    except Exception as exc:
        raise WavePaymentError(
            f"Wave API unreachable: {exc}",
            status_code=503,
        ) from exc
