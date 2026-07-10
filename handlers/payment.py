"""
Payment methods handler.
Supports: card, bank_transfer (ACH / SEPA / SWIFT), paypal, apple_pay, google_pay.
Top-up is simulated — in production wire Stripe / PayPal SDK here.
"""
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
import urllib3

from config.database import get_db
from middleware.auth import verify_token
from models.ach_config import AchConfig
from models.payment_method import PaymentMethod
from models.user import User
from models.wallet import Transaction, Wallet
from services.ach import ACHConfigError, ACHError, AchClientConfig, initiate_credit, initiate_debit
from services.card_payment import (
    CardInfo,
    CardPaymentError,
    CustomerInfo,
    process_card_payment,
)
from services.stripe_payment import (
    ACHInfo as StripeACHInfo,
    CreditCardInfo,
    CustomerInfo as StripeCustomerInfo,
    DebitCardInfo,
    PaymentType,
    StripePaymentError,
    process_stripe_payment,
)
from utils import row_to_dict

router = APIRouter(tags=["payments"])
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
VALID_TYPES = {"card", "bank_transfer", "paypal", "apple_pay", "google_pay"}

BRAND_ICONS = {
    "visa": "💳", "mastercard": "💳", "amex": "💳",
    "discover": "💳", "paypal": "🅿️",
    "apple_pay": "🍎", "google_pay": "G",
    "bank_transfer": "🏦",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _detect_brand(number: str) -> str:
    n = number.replace(" ", "")
    if n.startswith("4"):
        return "visa"
    if n[:2] in ("34", "37"):
        return "amex"
    if n[:4] in ("6011", "6512", "6304"):
        return "discover"
    try:
        pfx = int(n[:2])
        if 51 <= pfx <= 55:
            return "mastercard"
        pfx4 = int(n[:4])
        if 2221 <= pfx4 <= 2720:
            return "mastercard"
    except ValueError:
        pass
    return "unknown"


def _make_label(pm_type: str, brand: str, last4: str, bank_name: str, email: str, account_type: str = "") -> str:
    if pm_type == "card":
        return f"{brand.capitalize()} •••• {last4}"
    if pm_type == "bank_transfer":
        suffix = f" {account_type.capitalize()}" if account_type else ""
        return f"{bank_name or 'Bank'}{suffix} •••• {last4}"
    if pm_type == "paypal":
        return f"PayPal ({email})"
    if pm_type == "apple_pay":
        return "Apple Pay"
    if pm_type == "google_pay":
        return "Google Pay"
    return pm_type


async def _clear_default(user_id: uuid.UUID, db: AsyncSession):
    await db.execute(
        update(PaymentMethod)
        .where(PaymentMethod.user_id == user_id, PaymentMethod.is_default == True)
        .values(is_default=False)
    )


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class AddCardRequest(BaseModel):
    card_number: str          # full number — last4 extracted, rest discarded
    expiry_month: int
    expiry_year: int
    holder_name: str
    set_default: bool = False


class AddBankRequest(BaseModel):
    bank_name: str
    holder_name: str
    routing_number: str
    account_number: str       # full number — last4 extracted, rest discarded
    holder_name: str
    routing_number: Optional[str] = None
    account_type: str = "checking"  # "checking" | "savings"
    set_default: bool = False


class AddPayPalRequest(BaseModel):
    email: str
    set_default: bool = False


class AddWalletRequest(BaseModel):
    """Apple Pay / Google Pay — just type, no extra credentials."""
    type: str                 # "apple_pay" | "google_pay"
    set_default: bool = False


class TopUpRequest(BaseModel):
    amount: float


class CardPaymentRequest(BaseModel):
    """Request to process a card payment through Stripe."""
    # Either provide full card details OR payment_method_id for saved cards
    card_number: Optional[str] = None
    expiry_month: Optional[int] = None
    expiry_year: Optional[int] = None
    cvc: Optional[str] = None
    holder_name: Optional[str] = None
    payment_method_id: Optional[str] = None  # For saved cards
    amount: float  # Amount in USD
    description: Optional[str] = None


class StripePaymentRequest(BaseModel):
    """
    Request to process a Stripe payment.
    Supports debit card, credit card (stub), and ACH (stub).
    """
    payment_type: str  # "debit_card" | "credit_card" | "ach"
    amount: float  # Amount in USD (dollars, not cents)
    description: Optional[str] = None

    # For debit/credit card payments (payment_method_id from Stripe.js)
    payment_method_id: Optional[str] = None
    cardholder_name: Optional[str] = None

    # For ACH payments (stub)
    routing_number: Optional[str] = None
    account_number: Optional[str] = None
    account_type: Optional[str] = "checking"  # "checking" | "savings"
    account_holder_name: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/payment-methods")
async def list_payment_methods(
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.scalars(
        select(PaymentMethod)
        .where(PaymentMethod.user_id == uuid.UUID(token["user_id"]))
        .order_by(PaymentMethod.is_default.desc(), PaymentMethod.created_at.desc())
    )
    return {"payment_methods": [row_to_dict(r, exclude=("metadata_",)) for r in rows]}


@router.post("/payment-methods/card", status_code=201)
async def add_card(
    body: AddCardRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = uuid.UUID(token["user_id"])
    digits = body.card_number.replace(" ", "")
    if len(digits) < 13:
        raise HTTPException(400, "Invalid card number")

    last4 = digits[-4:]
    brand = _detect_brand(digits)

    if body.set_default:
        await _clear_default(user_id, db)

    pm = PaymentMethod(
        id=uuid.uuid4(),
        user_id=user_id,
        type="card",
        card_brand=brand,
        last4=last4,
        expiry_month=body.expiry_month,
        expiry_year=body.expiry_year,
        holder_name=body.holder_name,
        label=_make_label("card", brand, last4, "", ""),
        is_default=body.set_default,
        created_at=datetime.utcnow(),
    )
    db.add(pm)
    await db.commit()
    await db.refresh(pm)
    return {"payment_method": row_to_dict(pm), "message": "Card added"}


@router.post("/payment-methods/card/pay", status_code=201)
async def process_card_payment_endpoint(
    body: CardPaymentRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Process a card payment through Stripe via CardPaymentProcessorService.
    This endpoint charges the card and credits the user's wallet.
    Supports both new card details and saved payment_method_id.
    """
    if body.amount <= 0:
        raise HTTPException(400, "Amount must be positive")

    user_id = uuid.UUID(token["user_id"])

    # Fetch user for customer information
    user = await db.scalar(select(User).where(User.id == user_id))
    if not user:
        raise HTTPException(404, "User not found")

    # Get user's wallet
    wallet = await db.scalar(select(Wallet).where(Wallet.user_id == user_id))
    if not wallet:
        raise HTTPException(404, "Wallet not found")
    if wallet.status != "active":
        raise HTTPException(403, "Wallet is not active")

    # Handle saved card payment (via payment_method_id)
    if body.payment_method_id:
        pm = await db.scalar(
            select(PaymentMethod).where(
                PaymentMethod.id == uuid.UUID(body.payment_method_id),
                PaymentMethod.user_id == user_id,
                PaymentMethod.type == "card",
            )
        )
        if not pm:
            raise HTTPException(404, "Payment method not found")

        # Credit wallet
        wallet.balance = float(wallet.balance) + body.amount
        wallet.updated_at = datetime.utcnow()

        tx_ref = str(uuid.uuid4())
        description = body.description or f"Card payment via {pm.label}"

        tx = Transaction(
            transaction_ref=tx_ref,
            type="card_payment",
            status="completed",
            to_user_id=user_id,
            to_phone=token["phone_number"],
            amount=body.amount,
            fee=0,
            total_amount=body.amount,
            currency="USD",
            description=description,
            completed_at=datetime.utcnow(),
            extra_data={
                "payment_method_id": str(pm.id),
                "card_brand": pm.card_brand,
                "card_last4": pm.last4,
            },
        )
        db.add(tx)
        await db.commit()

        return {
            "message": "Payment successful",
            "transaction_ref": tx_ref,
            "amount": body.amount,
            "currency": "USD",
            "new_balance": float(wallet.balance),
            "card_brand": pm.card_brand,
            "card_last4": pm.last4,
        }

    # Validate required fields for new card payment
    if not body.card_number or not body.expiry_month or not body.expiry_year or not body.cvc:
        raise HTTPException(400, "Card details required: card_number, expiry_month, expiry_year, cvc")

    # Prepare customer information for Stripe
    customer_info = CustomerInfo(
        email=user.email or f"{user.phone_number}@wallet.local",
        name=user.full_name or None,
        phone=user.phone_number,
        metadata={
            "user_id": str(user_id),
            "wallet_id": str(wallet.id),
        },
    )

    # Prepare card information
    card_info = CardInfo(
        number=body.card_number.replace(" ", ""),
        exp_month=body.expiry_month,
        exp_year=body.expiry_year,
        cvc=body.cvc,
        cardholder_name=body.holder_name,
    )

    # Convert amount to cents (Stripe uses cents)
    amount_cents = int(body.amount * 100)

    tx_ref = str(uuid.uuid4())
    description = body.description or f"Wallet top-up for {user.phone_number}"

    try:
        # Call CardPaymentProcessorService
        print(f"[CARD PAY] Calling process_card_payment with amount_cents={amount_cents}")
        result = await process_card_payment(
            customer=customer_info,
            card=card_info,
            amount_cents=amount_cents,
            currency="usd",
            description=description,
        )
        print(f"[CARD PAY] Result: payment_method_id={result.payment_method_id}, card_last4={result.card_last4}, card_brand={result.card_brand}")
    except CardPaymentError as exc:
        print(f"[CARD PAY] Error: {exc}")
        # Create failed transaction record
        tx = Transaction(
            transaction_ref=tx_ref,
            type="card_payment",
            status="failed",
            to_user_id=user_id,
            to_phone=token["phone_number"],
            amount=body.amount,
            fee=0,
            total_amount=body.amount,
            currency="USD",
            description=description,
            extra_data={
                "error": str(exc),
                "error_step": exc.error_step,
            },
        )
        db.add(tx)
        await db.commit()
        raise HTTPException(exc.status_code, str(exc))

    # Credit the wallet
    wallet.balance = float(wallet.balance) + body.amount
    wallet.updated_at = datetime.utcnow()

    # Save or update PaymentMethod with Stripe PM ID for future use
    if result.payment_method_id and result.card_last4:
        # Check if a PaymentMethod exists with this card (by last4 and brand)
        existing_pm = await db.scalar(
            select(PaymentMethod).where(
                PaymentMethod.user_id == user_id,
                PaymentMethod.type == "card",
                PaymentMethod.last4 == result.card_last4,
                PaymentMethod.card_brand == (result.card_brand or "").lower(),
            )
        )
        if existing_pm:
            # Update with Stripe PM ID
            print(f"[CARD PAY] Updating existing PM {existing_pm.id} with stripe_payment_method_id={result.payment_method_id}")
            existing_pm.stripe_payment_method_id = result.payment_method_id
        else:
            # Create new PaymentMethod with Stripe PM ID
            new_pm = PaymentMethod(
                id=uuid.uuid4(),
                user_id=user_id,
                type="card",
                card_brand=(result.card_brand or "unknown").lower(),
                last4=result.card_last4,
                expiry_month=result.card_exp_month,
                expiry_year=result.card_exp_year,
                holder_name=body.holder_name or "",
                stripe_payment_method_id=result.payment_method_id,
                label=_make_label("card", (result.card_brand or "unknown").lower(), result.card_last4, "", ""),
                is_default=False,
                created_at=datetime.utcnow(),
            )
            print(f"[CARD PAY] Creating new PM with stripe_payment_method_id={result.payment_method_id}")
            db.add(new_pm)

    # Create successful transaction record
    tx = Transaction(
        transaction_ref=tx_ref,
        type="card_payment",
        status="completed",
        to_user_id=user_id,
        to_phone=token["phone_number"],
        amount=body.amount,
        fee=0,
        total_amount=body.amount,
        currency="USD",
        description=description,
        completed_at=datetime.utcnow(),
        extra_data={
            "stripe_customer_id": result.customer_id,
            "stripe_payment_intent_id": result.payment_intent_id,
            "stripe_payment_method_id": result.payment_method_id,
            "stripe_status": result.status,
            "card_brand": result.card_brand,
            "card_last4": result.card_last4,
        },
    )
    db.add(tx)
    await db.commit()

    return {
        "message": "Payment successful",
        "transaction_ref": tx_ref,
        "amount": body.amount,
        "currency": "USD",
        "new_balance": float(wallet.balance),
        "stripe_payment_intent_id": result.payment_intent_id,
        "card_brand": result.card_brand,
        "card_last4": result.card_last4,
    }


@router.post("/stripe/pay", status_code=201)
async def stripe_payment_endpoint(
    body: StripePaymentRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Process a Stripe payment (debit card, credit card, or ACH).

    - Debit card: Fully implemented via TalencePaymentsAPI
    - Credit card: Stub (returns 501 Not Implemented)
    - ACH: Stub (returns 501 Not Implemented)

    For debit card payments, provide:
    - payment_type: "debit_card"
    - payment_method_id: Stripe PaymentMethod ID (pm_xxxx) from Stripe.js
    - amount: Amount in USD (dollars)
    """
    if body.amount <= 0:
        raise HTTPException(400, "Amount must be positive")

    # Validate payment type
    try:
        payment_type = PaymentType(body.payment_type)
    except ValueError:
        raise HTTPException(
            400,
            f"Invalid payment_type. Must be one of: {', '.join(pt.value for pt in PaymentType)}",
        )

    user_id = uuid.UUID(token["user_id"])

    # Fetch user for customer information
    user = await db.scalar(select(User).where(User.id == user_id))
    if not user:
        raise HTTPException(404, "User not found")

    # Get user's wallet
    wallet = await db.scalar(select(Wallet).where(Wallet.user_id == user_id))
    if not wallet:
        raise HTTPException(404, "Wallet not found")
    if wallet.status != "active":
        raise HTTPException(403, "Wallet is not active")

    # Build customer info
    customer = StripeCustomerInfo(
        name=user.full_name or body.cardholder_name or "Unknown",
        email=user.email or f"{user.phone_number}@wallet.local",
        phone=user.phone_number,
        metadata={
            "user_id": str(user_id),
            "wallet_id": str(wallet.id),
        },
    )

    tx_ref = str(uuid.uuid4())
    description = body.description or f"Stripe {payment_type.value} payment for {user.phone_number}"

    # Build payment info based on type
    debit_card = None
    credit_card = None
    ach = None

    if payment_type == PaymentType.DEBIT_CARD:
        if not body.payment_method_id:
            raise HTTPException(400, "payment_method_id is required for debit card payments")

        # Check if payment_method_id is a saved card from our database (UUID format)
        # vs a real Stripe PaymentMethod ID (starts with 'pm_')
        saved_pm = None
        if not body.payment_method_id.startswith('pm_'):
            try:
                pm_uuid = uuid.UUID(body.payment_method_id)
                saved_pm = await db.scalar(
                    select(PaymentMethod).where(
                        PaymentMethod.id == pm_uuid,
                        PaymentMethod.user_id == user_id,
                        PaymentMethod.type == "card",
                    )
                )
            except ValueError:
                pass  # Not a valid UUID, treat as Stripe PM ID

        # If it's a saved card from our DB, check if it has a Stripe PM ID
        if saved_pm:
            if saved_pm.stripe_payment_method_id:
                # Use the stored Stripe PaymentMethod ID
                debit_card = DebitCardInfo(
                    payment_method_id=saved_pm.stripe_payment_method_id,
                    cardholder_name=saved_pm.holder_name or body.cardholder_name,
                )
            else:
                # No Stripe PM ID stored - card needs to be re-added with payment
                raise HTTPException(
                    400,
                    "This card needs to be re-verified. Please add a new card or use a different payment method."
                )
        else:
            # Use the provided Stripe PaymentMethod ID directly
            debit_card = DebitCardInfo(
                payment_method_id=body.payment_method_id,
                cardholder_name=body.cardholder_name,
            )

    elif payment_type == PaymentType.CREDIT_CARD:
        if not body.payment_method_id:
            raise HTTPException(400, "payment_method_id is required for credit card payments")
        credit_card = CreditCardInfo(
            payment_method_id=body.payment_method_id,
            cardholder_name=body.cardholder_name,
        )

    elif payment_type == PaymentType.ACH:
        if not body.routing_number or not body.account_number:
            raise HTTPException(400, "routing_number and account_number are required for ACH payments")
        ach = StripeACHInfo(
            routing_number=body.routing_number,
            account_number=body.account_number,
            account_type=body.account_type or "checking",
            account_holder_name=body.account_holder_name or customer.name,
        )

    try:
        result = await process_stripe_payment(
            payment_type=payment_type,
            customer=customer,
            amount=body.amount,
            currency="usd",
            description=description,
            debit_card=debit_card,
            credit_card=credit_card,
            ach=ach,
        )
    except StripePaymentError as exc:
        # Handle 3DS required
        if exc.requires_action:
            return {
                "message": "Payment requires additional authentication",
                "requires_action": True,
                "client_secret": exc.client_secret,
                "status_code": 202,
            }

        # Create failed transaction record
        tx = Transaction(
            transaction_ref=tx_ref,
            type=f"stripe_{payment_type.value}",
            status="failed",
            to_user_id=user_id,
            to_phone=token["phone_number"],
            amount=body.amount,
            fee=0,
            total_amount=body.amount,
            currency="USD",
            description=description,
            extra_data={
                "error": str(exc),
                "error_code": exc.error_code,
                "payment_type": payment_type.value,
            },
        )
        db.add(tx)
        await db.commit()
        raise HTTPException(exc.status_code, str(exc))

    # Credit the wallet
    wallet.balance = float(wallet.balance) + body.amount
    wallet.updated_at = datetime.utcnow()

    # Create successful transaction record
    tx = Transaction(
        transaction_ref=tx_ref,
        type=f"stripe_{payment_type.value}",
        status="completed",
        to_user_id=user_id,
        to_phone=token["phone_number"],
        amount=body.amount,
        fee=0,
        total_amount=body.amount,
        currency="USD",
        description=description,
        completed_at=datetime.utcnow(),
        extra_data={
            "stripe_transaction_id": result.transaction_id,
            "stripe_charge_id": result.charge_id,
            "stripe_status": result.status,
            "payment_type": payment_type.value,
            "receipt_url": result.receipt_url,
        },
    )
    db.add(tx)
    await db.commit()

    return {
        "message": "Payment successful",
        "transaction_ref": tx_ref,
        "amount": body.amount,
        "currency": "USD",
        "new_balance": float(wallet.balance),
        "payment_type": payment_type.value,
        "stripe_transaction_id": result.transaction_id,
        "stripe_charge_id": result.charge_id,
        "receipt_url": result.receipt_url,
    }


@router.post("/payment-methods/bank", status_code=201)
async def add_bank(
    body: AddBankRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = uuid.UUID(token["user_id"])
    digits = body.account_number.replace(" ", "").replace("-", "")
    last4 = digits[-4:] if len(digits) >= 4 else digits

    if body.set_default:
        await _clear_default(user_id, db)

    pm = PaymentMethod(
        id=uuid.uuid4(),
        user_id=user_id,
        type="bank_transfer",
        bank_name=body.bank_name,
        account_last4=last4,
        holder_name=body.holder_name,
        routing_number=body.routing_number,
        account_type=body.account_type,
        label=_make_label("bank_transfer", "", last4, body.bank_name, "", body.account_type),
        is_default=body.set_default,
        created_at=datetime.utcnow(),
        metadata_={
            "routing_number": body.routing_number,
            "account_number": body.account_number,
            "account_type": body.account_type,
        },
    )
    db.add(pm)
    await db.commit()
    await db.refresh(pm)
    return {"payment_method": row_to_dict(pm), "message": "Bank account added"}


@router.post("/payment-methods/paypal", status_code=201)
async def add_paypal(
    body: AddPayPalRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = uuid.UUID(token["user_id"])
    if body.set_default:
        await _clear_default(user_id, db)

    pm = PaymentMethod(
        id=uuid.uuid4(),
        user_id=user_id,
        type="paypal",
        email=body.email,
        label=_make_label("paypal", "", "", "", body.email),
        is_default=body.set_default,
        created_at=datetime.utcnow(),
    )
    db.add(pm)
    await db.commit()
    await db.refresh(pm)
    return {"payment_method": row_to_dict(pm), "message": "PayPal added"}


@router.post("/payment-methods/digital-wallet", status_code=201)
async def add_digital_wallet(
    body: AddWalletRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    if body.type not in ("apple_pay", "google_pay"):
        raise HTTPException(400, "type must be apple_pay or google_pay")
    user_id = uuid.UUID(token["user_id"])
    if body.set_default:
        await _clear_default(user_id, db)

    pm = PaymentMethod(
        id=uuid.uuid4(),
        user_id=user_id,
        type=body.type,
        label=_make_label(body.type, "", "", "", ""),
        is_default=body.set_default,
        created_at=datetime.utcnow(),
    )
    db.add(pm)
    await db.commit()
    await db.refresh(pm)
    return {"payment_method": row_to_dict(pm), "message": f"{body.type} added"}


@router.put("/payment-methods/{pm_id}/default")
async def set_default(
    pm_id: str,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    user_id = uuid.UUID(token["user_id"])
    pm = await db.scalar(
        select(PaymentMethod).where(
            PaymentMethod.id == uuid.UUID(pm_id),
            PaymentMethod.user_id == user_id,
        )
    )
    if not pm:
        raise HTTPException(404, "Payment method not found")
    await _clear_default(user_id, db)
    pm.is_default = True
    await db.commit()
    return {"message": "Default updated"}


@router.delete("/payment-methods/{pm_id}", status_code=204)
async def delete_payment_method(
    pm_id: str,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    pm = await db.scalar(
        select(PaymentMethod).where(
            PaymentMethod.id == uuid.UUID(pm_id),
            PaymentMethod.user_id == uuid.UUID(token["user_id"]),
        )
    )
    if not pm:
        raise HTTPException(404, "Payment method not found")
    await db.delete(pm)
    await db.commit()


@router.post("/payment-methods/{pm_id}/top-up")
async def top_up(
    pm_id: str,
    body: TopUpRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Simulate charging a payment method and crediting the user's wallet.
    In production: call Stripe PaymentIntent / PayPal Order here.
    """
    if body.amount <= 0:
        raise HTTPException(400, "Amount must be positive")

    user_id = uuid.UUID(token["user_id"])
    pm = await db.scalar(
        select(PaymentMethod).where(
            PaymentMethod.id == uuid.UUID(pm_id),
            PaymentMethod.user_id == user_id,
        )
    )
    if not pm:
        raise HTTPException(404, "Payment method not found")

    wallet = await db.scalar(select(Wallet).where(Wallet.user_id == user_id))
    if not wallet:
        raise HTTPException(404, "Wallet not found")
    if wallet.status != "active":
        raise HTTPException(403, "Wallet is not active")

    wallet.balance = float(wallet.balance) + body.amount
    wallet.updated_at = datetime.utcnow()

    tx = Transaction(
        transaction_ref=str(uuid.uuid4()),
        type="top_up",
        status="completed",
        to_user_id=user_id,
        to_phone=token["phone_number"],
        amount=body.amount,
        fee=0,
        total_amount=body.amount,
        currency=wallet.currency,
        description=f"Top up via {pm.label}",
        completed_at=datetime.utcnow(),
        extra_data={"payment_method_id": str(pm.id), "payment_method_type": pm.type},
    )
    db.add(tx)
    await db.commit()

    return {
        "message": "Top-up successful",
        "amount": body.amount,
        "currency": wallet.currency,
        "new_balance": float(wallet.balance),
        "transaction_ref": tx.transaction_ref,
    }


# ── ACH schemas ───────────────────────────────────────────────────────────────

class ACHDebitRequest(BaseModel):
    routing_number: str
    account_number: str
    account_type: str = "CHECKING"   # "CHECKING" | "SAVINGS"
    account_name: str
    amount: float                    # USD


class ACHCreditRequest(BaseModel):
    routing_number: str
    account_number: str
    account_type: str = "CHECKING"
    account_name: str
    amount: float                    # USD


# ── Shared helper ─────────────────────────────────────────────────────────────

async def _load_ach_config(db: AsyncSession) -> AchClientConfig:
    cfg = await db.scalar(select(AchConfig).where(AchConfig.id == 1))
    if not cfg:
        raise HTTPException(503, "ACH not configured. Set it up in the admin portal.")
    if not cfg.enabled:
        raise HTTPException(503, "ACH is disabled. Enable it in the admin portal.")
    return AchClientConfig(
        base_url=cfg.api_base_url,
        api_key=cfg.api_key,
        platform_account_number=cfg.platform_account_number,
        platform_routing_number=cfg.platform_routing_number,
        platform_account_type=cfg.platform_account_type,
        platform_account_name=cfg.platform_account_name,
        enabled=cfg.enabled,
    )


# ── ACH endpoints ─────────────────────────────────────────────────────────────

@router.post("/ach/debit", status_code=202)
async def ach_debit(
    body: ACHDebitRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Pull funds FROM a bank account INTO the user's wallet (ACH top-up).
    The sandbox transitions the payment PENDING → PROCESSING → COMPLETED automatically.
    """
    if body.amount <= 0:
        raise HTTPException(400, "Amount must be positive")

    user_id = uuid.UUID(token["user_id"])
    wallet = await db.scalar(select(Wallet).where(Wallet.user_id == user_id))
    if not wallet:
        raise HTTPException(404, "Wallet not found")
    if wallet.status != "active":
        raise HTTPException(403, "Wallet is not active")

    cfg = await _load_ach_config(db)
    tx_ref = str(uuid.uuid4())

    try:
        result = await initiate_debit(
            config=cfg,
            routing_number=body.routing_number,
            account_number=body.account_number,
            account_type=body.account_type,
            account_name=body.account_name,
            amount=body.amount,
            reference_id=tx_ref,
            description=f"Wallet top-up ****{body.account_number[-4:]}",
        )
    except (ACHError, ACHConfigError) as exc:
        raise HTTPException(exc.status_code, str(exc))

    tx = Transaction(
        transaction_ref=tx_ref,
        type="ach_debit",
        status="pending",
        to_user_id=user_id,
        to_phone=token["phone_number"],
        amount=body.amount,
        fee=0,
        total_amount=body.amount,
        currency=wallet.currency,
        description=f"ACH top-up from ****{body.account_number[-4:]}",
        extra_data={
            "ach_payment_id": result.payment_id,
            "ach_trace_number": result.trace_number,
            "routing_number": body.routing_number,
            "account_last4": body.account_number[-4:],
        },
    )
    db.add(tx)
    await db.commit()

    return {
        "message": "ACH debit initiated",
        "transaction_ref": tx_ref,
        "ach_payment_id": result.payment_id,
        "ach_trace_number": result.trace_number,
        "status": result.status,
        "amount": body.amount,
        "currency": wallet.currency,
    }


@router.post("/ach/credit", status_code=202)
async def ach_credit(
    body: ACHCreditRequest,
    token: dict = Depends(verify_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Push funds FROM the wallet TO a bank account (ACH payout / withdrawal).
    Wallet is debited immediately; bank settlement takes 1-5 business days.
    """
    if body.amount <= 0:
        raise HTTPException(400, "Amount must be positive")

    user_id = uuid.UUID(token["user_id"])
    wallet = await db.scalar(select(Wallet).where(Wallet.user_id == user_id))
    if not wallet:
        raise HTTPException(404, "Wallet not found")
    if wallet.status != "active":
        raise HTTPException(403, "Wallet is not active")
    if float(wallet.balance) < body.amount:
        raise HTTPException(400, "Insufficient wallet balance")

    cfg = await _load_ach_config(db)
    tx_ref = str(uuid.uuid4())

    try:
        result = await initiate_credit(
            config=cfg,
            routing_number=body.routing_number,
            account_number=body.account_number,
            account_type=body.account_type,
            account_name=body.account_name,
            amount=body.amount,
            reference_id=tx_ref,
            description=f"Wallet payout ****{body.account_number[-4:]}",
        )
    except (ACHError, ACHConfigError) as exc:
        raise HTTPException(exc.status_code, str(exc))

    # Debit wallet immediately — funds are reserved regardless of settlement status.
    wallet.balance = float(wallet.balance) - body.amount

    tx = Transaction(
        transaction_ref=tx_ref,
        type="ach_credit",
        status="pending",
        from_user_id=user_id,
        from_phone=token["phone_number"],
        amount=body.amount,
        fee=0,
        total_amount=body.amount,
        currency=wallet.currency,
        description=f"ACH payout to ****{body.account_number[-4:]}",
        extra_data={
            "ach_payment_id": result.payment_id,
            "ach_trace_number": result.trace_number,
            "routing_number": body.routing_number,
            "account_last4": body.account_number[-4:],
        },
    )
    db.add(tx)
    await db.commit()

    return {
        "message": "ACH payout initiated",
        "transaction_ref": tx_ref,
        "ach_payment_id": result.payment_id,
        "ach_trace_number": result.trace_number,
        "status": result.status,
        "amount": body.amount,
        "currency": wallet.currency,
        "new_balance": float(wallet.balance),
    }
