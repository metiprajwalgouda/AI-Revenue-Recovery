"""
Bridges a REAL CheckoutSession (from the live storefront) into the exact same
classify -> decide -> execute pipeline used by the synthetic batch runner.

This is the module that makes the live site and the batch pipeline share one
brain: no separate "live classifier logic" exists. A real abandoned checkout
and a synthetic one are processed by identical code from this point onward.
"""

import logging
from sqlalchemy.orm import Session

from app.models import CheckoutEvent, PaymentMethod
from app.db_models import CheckoutSession, RecoveryOutcomeRecord, SessionStatus
from app.agent.classifier import classify
from app.agent.recovery_actions import decide_action, execute_action

logger = logging.getLogger("recovery_agent.live_recovery")


def _session_to_checkout_event(session: CheckoutSession) -> CheckoutEvent:
    """Converts a DB-backed CheckoutSession into the CheckoutEvent shape the
    classifier expects. Real sessions have fewer known signals than synthetic
    ones -- that's expected and fine, the hybrid classifier already handles
    missing data correctly (see classifier.py's None-checks) rather than
    guessing from absent fields."""

    return CheckoutEvent(
        event_id=session.event_id,
        customer_id=f"cust_{session.id}",
        customer_email=session.customer_email,
        customer_phone=session.customer_phone,
        cart_value=session.cart_value,
        payment_method_attempted=None,  # not captured at the DB layer today; see CHALLENGES.md
        checkout_started_at=session.started_at,
        abandoned_at=session.abandoned_at,
        payment_status_code=session.payment_status_code,
        page_load_time_ms=session.page_load_time_ms,
        otp_requested=False,   # not currently captured from real Checkout.js -- see CHALLENGES.md
        otp_verified=False,
        time_on_checkout_page_sec=session.time_on_checkout_page_sec,
        notes=None,
        true_reason=None,  # unknown for real data -- this is what we're trying to INFER
        opted_out_of_marketing=session.opted_out_of_marketing,
        previous_recovery_attempts=session.previous_recovery_attempts,
    )


def run_recovery_for_session(
    session: CheckoutSession,
    db: Session,
    razorpay_client,
    llm_client=None,
) -> RecoveryOutcomeRecord:
    """Runs one real abandoned session through the full agent pipeline and
    persists the result. Idempotent guard: if a RecoveryOutcomeRecord already
    exists for this session (e.g. abandon endpoint called twice), returns the
    existing record instead of creating a duplicate or double-charging effort
    against the Razorpay link quota."""

    existing = db.query(RecoveryOutcomeRecord).filter(
        RecoveryOutcomeRecord.session_id == session.id
    ).first()
    if existing:
        logger.info(f"Recovery already ran for session {session.event_id}, returning existing record.")
        return existing

    event = _session_to_checkout_event(session)
    classification = classify(event, llm_client=llm_client)
    action = decide_action(event, classification)
    outcome = execute_action(event, classification, action, razorpay_client, reference_id=session.event_id)

    record = RecoveryOutcomeRecord(
        session_id=session.id,
        predicted_reason=classification.predicted_reason.value,
        confidence=classification.confidence,
        classification_method=classification.method_used,
        reasoning=classification.reasoning,
        action_taken=action.value,
        action_success=outcome.action_success,
        amount_offered=outcome.amount_offered,
        confirmed_recovered_amount=outcome.confirmed_recovered_amount,
        payment_link_id=outcome.payment_link_id,
        error_message=outcome.error_message,
    )
    db.add(record)
    session.previous_recovery_attempts += 1
    db.commit()
    db.refresh(record)

    return record