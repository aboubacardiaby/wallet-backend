# Import all models so Alembic autogenerate can discover them
from models.base import Base
from models.user import User, OTP
from models.wallet import Wallet, Agent, Transaction, MoneyRequest
from models.notification import Notification
from models.recipient import Recipient
from models.kyc import KYCSubmission
from models.payment_method import PaymentMethod

__all__ = ["Base", "User", "OTP", "Wallet", "Agent", "Transaction", "MoneyRequest", "Notification", "Recipient", "KYCSubmission", "PaymentMethod"]
