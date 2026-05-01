"""
OTP verification via Twilio Verify.

Twilio Verify handles code generation, SMS delivery, expiry, retry
limits, and fraud controls — nothing is stored locally.

Simulation mode: when TWILIO_ACCOUNT_SID or TWILIO_VERIFY_SERVICE_SID
are not set, a random code is generated, printed to stdout, and held
in an in-memory dict so the full register → verify flow works locally
without Twilio credentials.
"""
import os
import random

import httpx

_sim_store: dict[str, str] = {}


class VerifyError(Exception):
    pass


class VerificationNotFound(Exception):
    """No pending verification exists for this phone — must call send_verification first."""
    pass


async def send_verification(phone: str) -> str:
    """
    Trigger a Twilio Verify SMS to `phone` (E.164).

    Returns the Twilio verification SID (or a simulation placeholder),
    suitable for storing in the otps table as an audit record.

    Raises:
        VerifyError: if Twilio rejects the request or is unreachable.
    """
    account_sid  = os.getenv("TWILIO_ACCOUNT_SID", "")
    auth_token   = os.getenv("TWILIO_AUTH_TOKEN", "")
    service_sid  = os.getenv("TWILIO_VERIFY_SERVICE_SID", "")

    if not account_sid or not service_sid:
        code = f"{random.randint(0, 999999):06d}"
        _sim_store[phone] = code
        print(f"[VERIFY SIM] OTP for {phone}: {code}")
        return "SIM_VERIFY"

    url = f"https://verify.twilio.com/v2/Services/{service_sid}/Verifications"
    print(f"[VERIFY SEND] sending to={phone}")
    try:
        async with httpx.AsyncClient(auth=(account_sid, auth_token), timeout=10) as client:
            resp = await client.post(url, data={"To": phone, "Channel": "sms"})
            resp.raise_for_status()
            return resp.json().get("sid", "TWILIO_VERIFY")
    except httpx.HTTPStatusError as exc:
        raise VerifyError(f"Twilio Verify failed: {exc.response.text}") from exc
    except Exception as exc:
        raise VerifyError(f"Twilio Verify unreachable: {exc}") from exc



async def check_verification(phone: str, code: str) -> bool:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    service_sid = os.getenv("TWILIO_VERIFY_SERVICE_SID", "")

    if not account_sid or not auth_token or not service_sid:
        expected = _sim_store.pop(phone, None)
        return expected is not None and code == expected

    url = f"https://verify.twilio.com/v2/Services/{service_sid}/VerificationCheck"

    print(f"[VERIFY CHECK] checking to={phone}")

    try:
        async with httpx.AsyncClient(auth=(account_sid, auth_token), timeout=10) as client:
            resp = await client.post(
                url,
                data={
                    "To": phone,
                    "Code": code,
                },
            )
    except Exception as exc:
        raise VerifyError(f"Twilio Verify unreachable: {exc}") from exc

    print(f"[VERIFY CHECK] status={resp.status_code} body={resp.text}")

    if resp.status_code == 404:
        raise VerificationNotFound(
            "No pending verification found. Please request a new code."
        )

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise VerifyError(
            f"Twilio VerificationCheck failed: {exc.response.text}"
        ) from exc

    return resp.json().get("status") == "approved"