"""
Data models for the Checkout Drop-off Recovery Agent.
These define the shape of every abandoned checkout event and recovery record.
"""

from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime
from enum import Enum


class AbandonmentReason(str, Enum):
    """Ground-truth reason we injected into synthetic data.
    In real life you wouldn't know this — the agent has to INFER it.
    We keep it here only to later measure how accurate our classifier is."""
    CARD_DECLINED = "card_declined"
    OTP_TIMEOUT = "otp_timeout"
    PAGE_LOAD_SLOW = "page_load_slow"
    HIGH_AMOUNT_HESITATION = "high_amount_hesitation"
    NETWORK_DROP = "network_drop"
    PRICE_SHOCK_AT_CHECKOUT = "price_shock_at_checkout"  # e.g. shipping fee surprise
    ACCIDENTAL_CLOSE = "accidental_close"
    UNKNOWN = "unknown"


class PaymentMethod(str, Enum):
    CARD = "card"
    UPI = "upi"
    NETBANKING = "netbanking"
    WALLET = "wallet"


class CheckoutEvent(BaseModel):
    """A single abandoned checkout — this is what our agent receives as input."""
    event_id: str
    customer_id: str
    customer_email: str
    customer_phone: str
    cart_value: float = Field(..., gt=0)
    payment_method_attempted: PaymentMethod
    checkout_started_at: datetime
    abandoned_at: datetime
    # Signals the agent can use to infer the cause (this is realistic —
    # in production you'd get these from your payment gateway webhooks / frontend logs)
    payment_status_code: Optional[str] = None       # e.g. "insufficient_funds", None if never attempted
    page_load_time_ms: Optional[int] = None
    otp_requested: bool = False
    otp_verified: bool = False
    time_on_checkout_page_sec: Optional[int] = None
    notes: Optional[str] = None                      # messy free-text, e.g. support chat snippet
    # Ground truth for evaluation only — agent must NOT read this field
    true_reason: AbandonmentReason
    opted_out_of_marketing: bool = False
    previous_recovery_attempts: int = 0


class RecoveryAction(str, Enum):
    SEND_PAYMENT_LINK = "send_payment_link"
    OFFER_ALTERNATE_PAYMENT_METHOD = "offer_alternate_payment_method"
    SEND_DISCOUNT_NUDGE = "send_discount_nudge"
    SEND_REMINDER_SMS = "send_reminder_sms"
    NO_ACTION_RESPECT_OPT_OUT = "no_action_respect_opt_out"
    NO_ACTION_MAX_RETRIES_REACHED = "no_action_max_retries_reached"
    FLAG_FOR_MANUAL_REVIEW = "flag_for_manual_review"


class ClassificationResult(BaseModel):
    """Output of the classifier stage."""
    event_id: str
    predicted_reason: AbandonmentReason
    confidence: float = Field(..., ge=0, le=1)
    method_used: Literal["rule", "llm"]
    reasoning: str  # human-readable explanation, shown in audit trail


class RecoveryOutcome(BaseModel):
    """Final record after the agent has acted — this is what feeds the dashboard."""
    event_id: str
    classification: ClassificationResult
    action_taken: RecoveryAction
    action_success: bool
    recovered_amount: Optional[float] = None
    error_message: Optional[str] = None
    timestamp: datetime