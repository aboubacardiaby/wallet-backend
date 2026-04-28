import asyncio
import smtplib
import uuid as uuid_lib
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from config.smtp import _send_sync, resolve_smtp, send_email
from middleware.auth import verify_token
from models.notification import Notification
from models.user import User
from utils import row_to_dict

router = APIRouter(tags=["notifications"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class TransferNotifyRequest(BaseModel):
    """Payload the mobile app sends after a successful transfer."""
    to_phone:        Optional[str]   = None
    phone:           Optional[str]   = None   # alias accepted too
    user_id:         Optional[str]   = None
    transfer_type:   str             = "wallet"
    transaction_ref: str             = ""
    received_amount: Optional[float] = None
    recv_currency:   str             = ""
    sender_name:     Optional[str]   = None
    pickup_code:     Optional[str]   = None
    wave_ref:        Optional[str]   = None
    # generic fallback fields (also accepted)
    type:            Optional[str]   = None
    title:           Optional[str]   = None
    message:         Optional[str]   = None
    data:            Optional[dict]  = None
    send_email:      bool            = False


class SenderNotifyRequest(BaseModel):
    """Payload for notifying the sender (transfer-email endpoint)."""
    recipient_type:  str             = "sender"
    transfer_type:   str             = "wallet"
    transaction_ref: str             = ""
    send_amount:     Optional[float] = None
    send_currency:   str             = ""
    fee:             Optional[float] = None
    received_amount: Optional[float] = None
    recv_currency:   str             = ""
    recipient_name:  Optional[str]   = None
    exchange_rate:   Optional[float] = None
    pickup_code:     Optional[str]   = None


class SendEmailRequest(BaseModel):
    to:          str
    subject:     str
    body:        str             = ""   # plain text fallback
    html:        str             = ""   # HTML body (preferred)
    text:        str             = ""   # alias for body
    smtp_config: Optional[dict] = None  # client-supplied SMTP config


# ── Helpers ───────────────────────────────────────────────────────────────────

def _transfer_notification_text(body: TransferNotifyRequest) -> tuple[str, str]:
    """Build (title, message) for a recipient transfer notification."""
    amt = f"{body.received_amount} {body.recv_currency}".strip() if body.received_amount else "money"
    sender = body.sender_name or "Someone"
    if body.transfer_type == "cash_pickup":
        title   = "Cash pickup ready"
        message = f"{sender} sent you {amt}. Pickup code: {body.pickup_code or '—'}."
    elif body.transfer_type == "wave":
        title   = "Wave transfer received"
        message = f"{sender} sent you {amt} via Wave Mobile Money."
    else:
        title   = "You received money"
        message = f"{sender} sent you {amt} to your wallet."
    return title, message


async def _send_with_config(smtp: dict, to: str, subject: str, html: str):
    """Fire-and-forget email using given SMTP config dict."""
    if not smtp.get("host") or not smtp.get("from_email"):
        return
    try:
        await asyncio.to_thread(
            _send_sync,
            smtp["host"], int(smtp.get("port", 587)),
            smtp.get("username", ""), smtp.get("password", ""),
            smtp["from_email"], smtp.get("from_name", "Kalipeh Wallet"),
            smtp.get("use_tls", True), smtp.get("use_ssl", False),
            to, subject, html,
        )
    except Exception:
        pass  # email failures are non-fatal


# ── User endpoints ────────────────────────────────────────────────────────────

@router.get("/notifications")
async def get_notifications(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = uuid_lib.UUID(token["user_id"])

    result = await db.execute(
        select(Notification)
        .where(Notification.user_id == user_id)
        .order_by(Notification.created_at.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    )
    rows = result.scalars().all()

    unread_count = await db.scalar(
        select(func.count()).where(
            Notification.user_id == user_id,
            Notification.is_read == False,
        )
    )

    return {
        "notifications": [row_to_dict(n) for n in rows],
        "unread_count": unread_count,
        "page": page,
        "limit": limit,
    }


@router.put("/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = uuid_lib.UUID(token["user_id"])
    notification = await db.scalar(
        select(Notification).where(
            Notification.id == uuid_lib.UUID(notification_id),
            Notification.user_id == user_id,
        )
    )
    if not notification:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")

    notification.is_read = True
    notification.read_at = datetime.utcnow()
    await db.commit()
    return {"message": "Notification marked as read"}


@router.post("/notifications/recipient-notify", status_code=status.HTTP_201_CREATED)
async def notify_recipient(
    body: TransferNotifyRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Notify a transfer recipient (in-app notification).
    Called by the sender's app after a successful transfer.
    """
    # Resolve target user
    target_phone = body.to_phone or body.phone
    user = None
    if body.user_id:
        user = await db.scalar(select(User).where(User.id == uuid_lib.UUID(body.user_id)))
    elif target_phone:
        user = await db.scalar(select(User).where(User.phone_number == target_phone))

    if not user:
        # Recipient may not be registered — return 200 silently
        return {"message": "Recipient not found in system", "notification_id": None}

    # Build notification content
    if body.title and body.message:
        title, message = body.title, body.message
        notif_type = body.type or "general"
    else:
        title, message = _transfer_notification_text(body)
        notif_type = "transfer"

    notif = Notification(
        user_id=user.id,
        type=notif_type,
        title=title,
        message=message,
        data=body.data or {
            "transaction_ref": body.transaction_ref,
            "transfer_type":   body.transfer_type,
            "received_amount": body.received_amount,
            "recv_currency":   body.recv_currency,
            "pickup_code":     body.pickup_code,
        },
        created_at=datetime.utcnow(),
    )
    db.add(notif)
    await db.commit()
    await db.refresh(notif)

    return {"message": "Notification sent", "notification_id": str(notif.id)}


@router.post("/notifications/transfer-email", status_code=status.HTTP_200_OK)
async def transfer_email(
    body: SenderNotifyRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Send a transfer confirmation email to the sender.
    Non-blocking — returns 200 even if SMTP is not configured.
    """
    user_id = uuid_lib.UUID(token["user_id"])
    user = await db.scalar(select(User).where(User.id == user_id))
    if not user or not user.email:
        return {"message": "No email on file"}

    smtp = await resolve_smtp(db)
    if smtp.get("host") and smtp.get("from_email"):
        amt_line = (
            f"{body.send_amount} {body.send_currency}" if body.send_amount else "your transfer"
        )
        subject = f"Transfer receipt — {amt_line}"
        html = f"""
        <div style="font-family:sans-serif;max-width:520px;margin:40px auto;padding:32px;
                    border:1px solid #e5e7eb;border-radius:12px;">
          <h2 style="color:#0A1628;margin-top:0;">Transfer Sent ✓</h2>
          <p>Your transfer of <strong>{amt_line}</strong> to
             <strong>{body.recipient_name or '—'}</strong> was successful.</p>
          <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:16px;">
            {''.join(f'<tr><td style="padding:8px 0;color:#6b7280;border-bottom:1px solid #f3f4f6">{lbl}</td>'
                     f'<td style="padding:8px 0;text-align:right;font-weight:600;border-bottom:1px solid #f3f4f6">{val}</td></tr>'
                     for lbl, val in [
                         ("Fee",           f"{body.fee} {body.send_currency}" if body.fee is not None else "—"),
                         ("Recipient gets",f"{body.received_amount} {body.recv_currency}" if body.received_amount else "—"),
                         ("Reference",     body.transaction_ref[:20] + "…" if len(body.transaction_ref) > 20 else body.transaction_ref),
                     ] if val != "—")}
          </table>
          <hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0;">
          <p style="color:#9ca3af;font-size:12px;">KalipehWallet — automated receipt.</p>
        </div>
        """
        await _send_with_config(smtp, user.email, subject, html)

    return {"message": "Receipt email queued"}


@router.post("/email/send")
async def send_email_endpoint(
    body: SendEmailRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Send a one-off email.
    Accepts either a client-supplied smtp_config or falls back to the
    server-side SMTP configuration stored in the database / environment.
    """
    # Prefer client-supplied SMTP config (sent by the app's receipt flow)
    if body.smtp_config and body.smtp_config.get("host"):
        smtp = body.smtp_config
    else:
        smtp = await resolve_smtp(db)

    if not smtp.get("host") or not smtp.get("from_email"):
        raise HTTPException(
            400,
            "SMTP not configured. Set SMTP_HOST / SMTP_FROM_EMAIL in .env "
            "or configure via Profile → Settings → Email (SMTP).",
        )

    html_body = body.html or body.body or body.text
    if not html_body:
        raise HTTPException(400, "No email body provided (html / body / text)")

    # Wrap plain text in minimal HTML if no tags present
    if "<" not in html_body:
        html_body = f"<p>{html_body.replace(chr(10), '<br>')}</p>"

    try:
        await asyncio.to_thread(
            _send_sync,
            smtp["host"], int(smtp.get("port", 587)),
            smtp.get("username", ""), smtp.get("password", ""),
            smtp.get("from_email", ""), smtp.get("from_name", "Kalipeh Wallet"),
            smtp.get("use_tls", True), smtp.get("use_ssl", False),
            body.to, body.subject, html_body,
        )
    except smtplib.SMTPAuthenticationError:
        raise HTTPException(400, "SMTP authentication failed — check username/password")
    except smtplib.SMTPRecipientsRefused:
        raise HTTPException(400, f"Recipient rejected: {body.to}")
    except smtplib.SMTPException as e:
        raise HTTPException(400, f"SMTP error: {e}")
    except OSError as e:
        raise HTTPException(400, f"Network error connecting to SMTP: {e}")
    except Exception as e:
        raise HTTPException(400, f"Failed to send email: {e}")

    return {"message": f"Email sent to {body.to}"}
