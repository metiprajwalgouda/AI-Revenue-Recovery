"""
Decides WHAT to do about an abandoned checkout, then actually does it.

Flow: guardrails (hard stop) -> classification (why did they leave?) ->
action decision (what do we do about it?) -> execution (actually do it via Razorpay).

Kept as two separate functions (decide, execute) on purpose: decide_action has
NO side effects and is trivially unit-testable with plain assertions.
execute_action is the only function that touches the network.
"""

import logging
from datetime import datetime, timezone

from app.models import (
    CheckoutEvent, ClassificationResult, AbandonmentReason,
    RecoveryAction, RecoveryOutcome,
)
from app.guardrails import check_guardrails, cap_discount, MAX_RECOVERY_ATTEMPTS
from app.razorpay_client import RazorpayRecoveryClient

logger = logging.getLogger("recovery_agent.recovery_actions")

# Confidence below this means: don't act on the LLM/rule's guess, get a human involved instead.
MIN_CONFIDENCE_TO_ACT = 0.5

# Reason -> default action, when confidence is high enough to act on it
REASON_ACTION_MAP = {
    AbandonmentReason.CARD_DECLINED: RecoveryAction.OFFER_ALTERNATE_PAYMENT_METHOD,
    AbandonmentReason.OTP_TIMEOUT: RecoveryAction.SEND_PAYMENT_LINK,
    AbandonmentReason.PAGE_LOAD_SLOW: RecoveryAction.SEND_PAYMENT_LINK,
    AbandonmentReason.NETWORK_DROP: RecoveryAction.SEND_PAYMENT_LINK,
    AbandonmentReason.HIGH_AMOUNT_HESITATION: RecoveryAction.SEND_DISCOUNT_NUDGE,
    AbandonmentReason.PRICE_SHOCK_AT_CHECKOUT: RecoveryAction.SEND_DISCOUNT_NUDGE,
    AbandonmentReason.ACCIDENTAL_CLOSE: RecoveryAction.SEND_REMINDER_SMS,
    AbandonmentReason.UNKNOWN: RecoveryAction.FLAG_FOR_MANUAL_REVIEW,
}

# Discount % offered per reason (before the hard cap is applied) -- only reasons
# that map to SEND_DISCOUNT_NUDGE actually use this.
DISCOUNT_BY_REASON = {
    AbandonmentReason.HIGH_AMOUNT_HESITATION: 10,
    AbandonmentReason.PRICE_SHOCK_AT_CHECKOUT: 20,  # intentionally set ABOVE the cap,
                                                      # to prove cap_discount() actually clamps it
}


def decide_action(event: CheckoutEvent, classification: ClassificationResult) -> RecoveryAction:
    """Pure decision function: given an event + its classification, what should we do?
    Guardrails are checked FIRST and can override everything else."""

    guardrail_action = check_guardrails(event)
    if guardrail_action is not None:
        return guardrail_action

    if classification.confidence < MIN_CONFIDENCE_TO_ACT:
        # Low-confidence guess (whether from a rule or the LLM) should not
        # trigger an automated money-touching action -- flag it instead.
        return RecoveryAction.FLAG_FOR_MANUAL_REVIEW

    return REASON_ACTION_MAP.get(classification.predicted_reason, RecoveryAction.FLAG_FOR_MANUAL_REVIEW)


def execute_action(
    event: CheckoutEvent,
    classification: ClassificationResult,
    action: RecoveryAction,
    razorpay_client: RazorpayRecoveryClient,
    reference_id: str | None = None,
) -> RecoveryOutcome:
    """Actually performs the recovery action. This is the only function with
    side effects (network calls) -- everything upstream of it is pure/testable.

    reference_id defaults to event.event_id but can be overridden (e.g. with a
    per-run suffix) since Razorpay treats reference_id as globally unique forever --
    see run_pipeline.py's module docstring for why this matters in practice."""

    reference_id = reference_id or event.event_id
    now = datetime.now(timezone.utc)

    # No-op actions: guardrail-driven, nothing to execute against Razorpay.
    if action in (
        RecoveryAction.NO_ACTION_RESPECT_OPT_OUT,
        RecoveryAction.NO_ACTION_MAX_RETRIES_REACHED,
        RecoveryAction.FLAG_FOR_MANUAL_REVIEW,
    ):
        return RecoveryOutcome(
            event_id=event.event_id,
            classification=classification,
            action_taken=action,
            action_success=True,  # "success" here means: correctly took no automated action
            timestamp=now,
        )

    if action == RecoveryAction.SEND_REMINDER_SMS:
        # Out of scope to wire a real SMS provider for this build --
        # logged honestly as a simulated action rather than faked as a real send.
        return RecoveryOutcome(
            event_id=event.event_id,
            classification=classification,
            action_taken=action,
            action_success=True,
            error_message="SIMULATED: SMS provider not integrated in this build.",
            timestamp=now,
        )

    # Remaining actions all funnel through Razorpay payment link creation,
    # optionally with a discount applied to the amount first.
    amount = event.cart_value
    if action == RecoveryAction.SEND_DISCOUNT_NUDGE:
        requested_discount = DISCOUNT_BY_REASON.get(classification.predicted_reason, 0)
        applied_discount = cap_discount(requested_discount)
        amount = round(amount * (1 - applied_discount / 100), 2)

    result = razorpay_client.create_recovery_payment_link(
        amount_rupees=amount,
        customer_name=event.customer_id,  # synthetic data has no real name field
        customer_email=event.customer_email,
        customer_phone=event.customer_phone,
        description=f"Complete your purchase - {action.value}",
        reference_id=reference_id,
    )

    return RecoveryOutcome(
        event_id=event.event_id,
        classification=classification,
        action_taken=action,
        action_success=result.success,
        # This is an OFFER, not confirmed revenue -- see the docstring on
        # RecoveryOutcome. confirmed_recovered_amount only gets set later,
        # by a separate reconciliation step that checks actual payment status.
        amount_offered=amount if result.success else None,
        payment_link_id=result.payment_link_id,
        error_message=result.error_message,
        timestamp=now,
    )