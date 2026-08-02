import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config.database import get_db
from middleware.auth import require_role, verify_token
from models.kyc import KYCSubmission
from models.user import User
from utils import row_to_dict
from utils.audit import log_audit

router = APIRouter(tags=["kyc"])


# ── Pydantic schemas ────────────────────────────────────────────────────────

class KYCSubmitRequest(BaseModel):
    # Personal info
    full_name: str
    date_of_birth: str          # YYYY-MM-DD
    nationality: str
    address: str
    city: str
    region: Optional[str] = ""
    country: str
    # Document
    id_type: str                # national_id | passport | drivers_license | residence_permit
    id_number: str
    id_expiry: str              # YYYY-MM-DD
    id_front_url: str           # base64 data-URI
    id_back_url: str
    selfie_url: str


class KYCReviewRequest(BaseModel):
    action: str                 # "approve" | "reject"
    rejection_reason: Optional[str] = None
    reviewer_id: str            # admin identifier (e.g. email or "admin")


# ── Helpers ─────────────────────────────────────────────────────────────────

_ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
_MAX_DOC_BYTES = 5 * 1024 * 1024  # 5 MB


def _validate_doc_url(value: str, field: str) -> None:
    if not value:
        raise HTTPException(status_code=422, detail=f"{field} is required")
    # Plain HTTPS URLs (e.g. S3 pre-signed) are allowed but validated loosely
    if value.startswith("https://"):
        if len(value) > 4096:
            raise HTTPException(status_code=422, detail=f"{field} URL is too long")
        return
    # Expect a data-URI upload
    m = re.match(r"^data:([^;]+);base64,([A-Za-z0-9+/=]+)$", value)
    if not m:
        raise HTTPException(
            status_code=422,
            detail=f"{field} must be a base64 data URI or HTTPS URL",
        )
    mime, b64 = m.group(1), m.group(2)
    if mime not in _ALLOWED_MIME:
        raise HTTPException(
            status_code=422,
            detail=f"{field} has unsupported file type: {mime}. Allowed: {', '.join(_ALLOWED_MIME)}",
        )
    # base64 string length * 0.75 is an upper-bound on raw byte size
    if len(b64) * 0.75 > _MAX_DOC_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"{field} exceeds 5 MB limit",
        )


async def _latest_submission(user_id: uuid.UUID, db: AsyncSession) -> Optional[KYCSubmission]:
    return await db.scalar(
        select(KYCSubmission)
        .where(KYCSubmission.user_id == user_id)
        .order_by(KYCSubmission.submitted_at.desc())
    )


# ── User endpoints ───────────────────────────────────────────────────────────

@router.get("/kyc/status")
async def get_kyc_status(
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = uuid.UUID(token["user_id"])
    user = await db.scalar(select(User).where(User.id == user_id))
    submission = await _latest_submission(user_id, db)

    return {
        "kyc_status": user.kyc_status if user else "pending",
        "submission": row_to_dict(submission) if submission else None,
    }


@router.post("/kyc/submit", status_code=status.HTTP_201_CREATED)
async def submit_kyc(
    body: KYCSubmitRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = uuid.UUID(token["user_id"])
    user = await db.scalar(select(User).where(User.id == user_id))
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.kyc_status == "verified":
        raise HTTPException(status_code=400, detail="KYC already verified")

    _validate_doc_url(body.id_front_url, "id_front_url")
    _validate_doc_url(body.id_back_url, "id_back_url")
    _validate_doc_url(body.selfie_url, "selfie_url")

    # If there is an existing pending/under_review submission, replace it
    existing = await _latest_submission(user_id, db)
    if existing and existing.status in ("pending", "under_review"):
        await db.delete(existing)

    now = datetime.utcnow()
    sub = KYCSubmission(
        id=uuid.uuid4(),
        user_id=user_id,
        full_name=body.full_name,
        date_of_birth=body.date_of_birth,
        nationality=body.nationality,
        address=body.address,
        city=body.city,
        region=body.region,
        country=body.country,
        id_type=body.id_type,
        id_number=body.id_number,
        id_expiry=body.id_expiry,
        id_front_url=body.id_front_url,
        id_back_url=body.id_back_url,
        selfie_url=body.selfie_url,
        status="pending",
        submitted_at=now,
        updated_at=now,
    )
    db.add(sub)

    # Sync personal info back to user profile
    user.full_name = body.full_name
    user.date_of_birth = (
        datetime.strptime(body.date_of_birth, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if body.date_of_birth else None
    )
    user.national_id_type = body.id_type
    user.national_id_number = body.id_number
    user.street = body.address
    user.city = body.city
    user.region = body.region
    user.country = body.country
    user.kyc_status = "pending"
    user.updated_at = now

    await db.commit()
    await db.refresh(sub)
    return {"message": "KYC submitted successfully", "submission_id": str(sub.id), "status": sub.status}


# ── Admin endpoints ──────────────────────────────────────────────────────────

@router.get("/admin/kyc/submissions")
async def list_kyc_submissions(
    kyc_status: Optional[str] = None,
    token: dict = Depends(require_role("super_admin", "compliance")),
    db: AsyncSession = Depends(get_db),
):
    """List all KYC submissions (admin). Filter by status if provided."""
    q = select(KYCSubmission).order_by(KYCSubmission.submitted_at.desc())
    if kyc_status:
        q = q.where(KYCSubmission.status == kyc_status)
    rows = await db.scalars(q)
    return {"submissions": [row_to_dict(r) for r in rows]}


@router.put("/admin/kyc/submissions/{submission_id}/review")
async def review_kyc(
    submission_id: str,
    body: KYCReviewRequest,
    request: Request,
    token: dict = Depends(require_role("super_admin", "compliance")),
    db: AsyncSession = Depends(get_db),
):
    """Approve or reject a KYC submission (admin)."""
    if body.action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="action must be 'approve' or 'reject'")
    if body.action == "reject" and not body.rejection_reason:
        raise HTTPException(status_code=400, detail="rejection_reason required when rejecting")

    sub = await db.scalar(
        select(KYCSubmission).where(KYCSubmission.id == uuid.UUID(submission_id))
    )
    if not sub:
        raise HTTPException(status_code=404, detail="Submission not found")

    now = datetime.utcnow()
    new_status = "verified" if body.action == "approve" else "rejected"
    sub.status = new_status
    sub.reviewed_by = body.reviewer_id
    sub.reviewed_at = now
    sub.rejection_reason = body.rejection_reason if body.action == "reject" else None
    sub.updated_at = now

    # Mirror status to the user
    user = await db.scalar(select(User).where(User.id == sub.user_id))
    if user:
        user.kyc_status = new_status
        user.updated_at = now

    await db.commit()

    await log_audit(
        db,
        action=f"kyc_{body.action}",
        actor_id=token.get("user_id") or body.reviewer_id,
        resource_type="kyc_submission",
        resource_id=submission_id,
        details={
            "reviewer_id": body.reviewer_id,
            "rejection_reason": body.rejection_reason,
            "user_id": str(user.id) if user else None,
        },
        ip_address=request.client.host if request.client else None,
    )

    return {"message": f"KYC {new_status}", "submission_id": submission_id, "status": new_status}
