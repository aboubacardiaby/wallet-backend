"""
SMS delivery via Twilio Programmable Messaging.

Uses httpx directly against the Twilio REST API to stay async-native
without pulling in the synchronous twilio SDK.

Simulation mode: when TWILIO_ACCOUNT_SID is not set the message is
printed to stdout so local development works without credentials.
"""
import os

import httpx


class SMSError(Exception):
    pass


async def send_sms(*, to: str, body: str) -> None:
    """
    Send an SMS to `to` (E.164 format) with the given `body`.

    Raises:
        SMSError: if Twilio rejects the request or is unreachable.
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    auth_token  = os.getenv("TWILIO_AUTH_TOKEN", "")
    from_number = os.getenv("TWILIO_FROM_NUMBER", "")

    if not account_sid:
        # Dev / simulation mode
        print(f"[SMS] To={to} | {body}")
        return

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    try:
        async with httpx.AsyncClient(auth=(account_sid, auth_token), timeout=10) as client:
            resp = await client.post(url, data={"From": from_number, "To": to, "Body": body})
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise SMSError(f"Twilio rejected SMS: {exc.response.text}") from exc
    except Exception as exc:
        raise SMSError(f"Twilio unreachable: {exc}") from exc
