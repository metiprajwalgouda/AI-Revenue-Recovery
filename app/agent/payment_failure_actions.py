"""
Payment Failure Action Ladder
==============================
Decides what to do given a PaymentFailureClassification and executes it,
creating/updating RecoveryCase and RecoveryActionLog rows.

Design constraints (per user spec):
  - risk_terminal OR confidence < 0.5  → ESCALATE, no comms sent
  - needs_customer_action              → SUPPRESSED (UX-only, logged only)
  - needs_alternate_method /
    retryable_technical               → create Razorpay payment link,
                                        increment contact_touches
  - contact_touches >= 3              → LOST, circuit breaker trips, no action
  - amount_offered < amount_at_risk
    OR coupon_code attached           → pending_approval, do NOT send
  - discount_allowed                  → ALWAYS False for payment failures
                                        (enforced at classification layer and
                                        re-checked here for defence-in-depth)

No abandonment code is touched. This module is self-contained.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.db_models import (
    RecoveryCase, RecoveryActionLog,
    RecoveryScenario, CaseStatus, ClassificationMethod,
    CheckoutSession, MerchantUser,
)
from app.agent.payment_failure_classifier import PaymentFailureClassification
from app.email_service import send_recovery_email
from app.notification_service import send_in_app_notification

logger = logging.getLogger("recovery_agent.payment_failure_actions")

CONTACT_TOUCH_LIMIT = 3  # circuit breaker per case


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _merchant_for_session(session: CheckoutSession, db: Session) -> Optional[MerchantUser]:
    """Resolves the merchant who owns the first product in the session's cart."""
    if not session.cart_json:
        return None
    try:
        from app.db_models import Product
        items = json.loads(session.cart_json)
        if not items:
            return None
        pid = items[0].get("product_id")
        product = db.query(Product).filter(Product.id == pid).first()
        if product:
            return db.query(MerchantUser).filter(MerchantUser.id == product.merchant_id).first()
    except Exception as exc:
        logger.warning("Could not resolve merchant for session %s: %s", session.event_id, exc)
    return None


def _get_or_create_case(
    session: CheckoutSession,
    classification: PaymentFailureClassification,
    db: Session,
) -> RecoveryCase:
    """Returns the existing RecoveryCase for this session if one already exists,
    otherwise creates a new one with status=AT_RISK and all classification metadata
    filled in.

    Idempotent: safe to call multiple times; subsequent calls for the same
    checkout_session_id return the existing row without overwriting status."""
    existing = (
        db.query(RecoveryCase)
        .filter(
            RecoveryCase.checkout_session_id == session.id,
            RecoveryCase.scenario == RecoveryScenario.PAYMENT_FAILURE,
        )
        .first()
    )
    if existing:
        db.expire(existing)  # force re-load from DB on next attribute access
                             # (otherwise contact_touches/ladder_step may be
                             #  stale ORM in-memory values from a previous call
                             #  in the same session, causing the circuit breaker
                             #  to never see the accumulated touch count)
        return existing

    merchant = _merchant_for_session(session, db)
    if merchant:
        merchant_id = merchant.id
    else:
        merchant_id = 1  # fallback to first merchant (demo store)
        logger.warning(
            "merchant_id resolution failed for session %s (cart_json=%r) — "
            "falling back to merchant_id=1. This case will be orphaned and "
            "invisible to the real merchant's dashboard. Fix cart/product linkage.",
            session.id, session.cart_json,
        )

    # Compute a simple RAR score: higher cart value + known-bad error class = higher priority
    rar_scores = {
        "risk_terminal": 100.0,
        "needs_alternate_method": 75.0,
        "retryable_technical": 60.0,
        "needs_customer_action": 40.0,
        "unknown": 30.0,
    }
    rar_score = rar_scores.get(classification.failure_class, 30.0)
    # Bump by value: +25 for high-value sessions (>= 5000)
    if session.cart_value >= 5000:
        rar_score = min(100.0, rar_score + 25.0)

    case = RecoveryCase(
        merchant_id=merchant_id,
        customer_user_id=session.customer_user_id,
        checkout_session_id=session.id,
        scenario=RecoveryScenario.PAYMENT_FAILURE,
        amount_at_risk=session.cart_value,
        amount_recovered=0.0,
        status=CaseStatus.AT_RISK,
        ladder_step=0,
        classification=classification.failure_class,
        classification_source=(
            ClassificationMethod.RULE if classification.method == "rule"
            else ClassificationMethod.LLM
        ),
        error_source=classification.source,
        rar_score=rar_score,
        confidence=classification.confidence,
        escalated_to_human=False,
        contact_touches=0,
    )
    db.add(case)
    db.flush()  # assign case.id before we write the action log
    return case


def _write_action_log(
    case: RecoveryCase,
    action_type: str,
    outcome: str,
    reason: str,
    guardrail_checks: dict,
    db: Session,
    amount_offered: Optional[float] = None,
    coupon_code: Optional[str] = None,
    requires_human_approval: bool = False,
) -> Optional[RecoveryActionLog]:
    """Creates a RecoveryActionLog row with the idempotency key
    '{case_id}:{ladder_step}:{action_type}'. Returns None (without raising)
    if the key already exists, so retry-safe callers don't need try/except."""
    idempotency_key = f"{case.id}:{case.ladder_step}:{action_type}"
    log = RecoveryActionLog(
        case_id=case.id,
        idempotency_key=idempotency_key,
        ladder_step=case.ladder_step,
        action_type=action_type,
        reason=reason,
        guardrail_checks=json.dumps(guardrail_checks),
        outcome=outcome,
        amount_offered=amount_offered,
        coupon_code=coupon_code,
        requires_human_approval=requires_human_approval,
    )
    db.add(log)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        logger.info(
            "Idempotency key %s already exists — skipping duplicate action log.",
            idempotency_key,
        )
        return None
    return log


def _send_payment_link_notification(
    session: CheckoutSession,
    case: RecoveryCase,
    link_url: str,
    merchant: Optional[MerchantUser],
    db: Session,
) -> None:
    """Fires an in-app notification and recovery email with the payment link.
    Intentionally non-fatal: if the email service is unavailable the payment
    link creation and case update are NOT rolled back."""
    customer_name = session.customer_name or "Customer"
    store_name = (merchant.store_name if merchant else None) or "Our Store"
    resume_url = link_url

    # In-app notification (persisted even if WebSocket is disconnected)
    if session.customer_user_id:
        try:
            send_in_app_notification(
                db,
                session.customer_user_id,
                "Complete your payment",
                f"Your payment was not completed. Tap to try again with a fresh payment link.",
                resume_url,
            )
        except Exception as exc:
            logger.warning("In-app notification failed for case %s: %s", case.id, exc)

    # Recovery email
    try:
        cart_items = []
        if session.cart_json:
            from app.db_models import Product
            raw = json.loads(session.cart_json)
            for ri in raw:
                p = db.query(Product).filter(Product.id == ri.get("product_id")).first()
                if p:
                    cart_items.append({
                        "name": p.name, "price": p.price,
                        "quantity": ri.get("quantity", 1),
                    })

        send_recovery_email(
            session.customer_email,
            "Your payment didn't go through — here's a fresh link",
            "emails/recovery_email.html",
            {
                "store_name": store_name,
                "customer_name": customer_name,
                "cart_items": cart_items,
                "subtotal": session.cart_value,
                "discount_amount": 0,  # NEVER discount payment failures
                "coupon_code": None,   # NEVER attach coupon to payment failures
                "total": session.cart_value,
                "resume_url": resume_url,
                "message": "There was a problem with your payment. Please use the link below to try again.",
            },
        )
    except Exception as exc:
        logger.warning("Recovery email failed for case %s: %s", case.id, exc)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_payment_failure_recovery(
    session: CheckoutSession,
    classification: PaymentFailureClassification,
    db: Session,
    razorpay_client=None,
) -> RecoveryCase:
    """Executes the payment-failure action ladder for one CheckoutSession.

    Returns the RecoveryCase (created or updated). Idempotent: if a case already
    exists and is not in a terminal state (RECOVERED, LOST, ESCALATED), it
    evaluates the next ladder step. If already terminal, returns immediately.

    Args:
      session         - CheckoutSession ORM row (already committed, status=ABANDONED
                        or STARTED with a payment failure signal set).
      classification  - Output of classify_payment_failure().
      db              - Active SQLAlchemy session.
      razorpay_client - RazorpayRecoveryClient or SimulatedRazorpayClient.
                        If None, falls back to SimulatedRazorpayClient (safe for tests).
    """
    if razorpay_client is None:
        from app.razorpay_client import SimulatedRazorpayClient
        razorpay_client = SimulatedRazorpayClient()
        logger.warning(
            "run_payment_failure_recovery called with no razorpay_client — "
            "using SimulatedRazorpayClient. Set RECOVERY_MODE=live for production."
        )

    now = datetime.now(timezone.utc)

    # ── 1. Get or create the RecoveryCase ────────────────────────────────────
    case = _get_or_create_case(session, classification, db)

    # Early exit: already in terminal state
    if case.status in (CaseStatus.RECOVERED, CaseStatus.LOST, CaseStatus.ESCALATED):
        logger.info(
            "Case %s already in terminal state %s — skipping.", case.id, case.status
        )
        db.commit()
        return case

    # ── 2. Branch on failure class ───────────────────────────────────────────

    failure_class = classification.failure_class
    confidence = classification.confidence

    # ── BRANCH A: risk_terminal OR low confidence → escalate to human ────────
    if failure_class == "risk_terminal" or confidence < 0.5:
        escalation_reason = (
            "risk_block" if failure_class == "risk_terminal" else "low_confidence"
        )
        guardrails = {
            "is_risk_terminal": failure_class == "risk_terminal",
            "confidence_below_threshold": confidence < 0.5,
            "confidence": confidence,
        }

        case.status = CaseStatus.ESCALATED
        case.escalated_to_human = True
        case.escalation_reason = escalation_reason
        case.last_action_at = now
        case.ladder_step += 1

        _write_action_log(
            case=case,
            action_type="human_escalation",
            outcome="sent",
            reason=(
                f"Risk terminal: {classification.description}"
                if failure_class == "risk_terminal"
                else f"Low confidence ({confidence:.2f}) — cannot act safely without human review."
            ),
            guardrail_checks=guardrails,
            db=db,
            requires_human_approval=False,
        )
        db.commit()
        logger.info(
            "Case %s escalated to human (reason=%s).", case.id, escalation_reason
        )
        return case

    # ── BRANCH B: needs_customer_action → UX-only, log suppressed ────────────
    if failure_class == "needs_customer_action":
        guardrails = {
            "reason": "needs_customer_action",
            "action": "suppressed — handled client-side by in-checkout retry prompt",
        }
        case.status = CaseStatus.INTERVENING
        case.last_action_at = now
        case.ladder_step += 1

        _write_action_log(
            case=case,
            action_type="ux_suppressed",
            outcome="suppressed",
            reason=(
                f"Customer-action failure ({classification.status_code}). "
                "No backend action needed — the in-checkout UI already prompts retry. "
                f"Description: {classification.description}"
            ),
            guardrail_checks=guardrails,
            db=db,
        )
        db.commit()
        logger.info(
            "Case %s: needs_customer_action — logged suppressed, no backend action.", case.id
        )
        return case

    # ── BRANCH C: needs_alternate_method | retryable_technical ───────────────
    if failure_class in ("needs_alternate_method", "retryable_technical"):

        # Circuit breaker
        if case.contact_touches >= CONTACT_TOUCH_LIMIT:
            guardrails = {
                "contact_touch_limit": CONTACT_TOUCH_LIMIT,
                "contact_touches": case.contact_touches,
                "circuit_breaker": "tripped",
            }
            case.status = CaseStatus.LOST
            case.last_action_at = now
            case.ladder_step += 1

            _write_action_log(
                case=case,
                action_type="circuit_breaker_stop",
                outcome="suppressed",
                reason=(
                    f"contact_touches ({case.contact_touches}) >= limit ({CONTACT_TOUCH_LIMIT}). "
                    "Max outreach attempts reached — marking case LOST."
                ),
                guardrail_checks=guardrails,
                db=db,
            )
            db.commit()
            logger.info(
                "Case %s: circuit breaker tripped (touches=%d) — marked LOST.",
                case.id, case.contact_touches,
            )
            return case

        # Discount guard (defence-in-depth — classification layer already enforces this,
        # but we re-check here in case a future code path bypasses the classifier).
        if classification.discount_allowed:
            # Should never happen for payment failures, but if it does: require approval.
            guardrails = {
                "discount_allowed": True,
                "gate": "requires_human_approval",
                "reason": "Unexpected discount flag on payment_failure scenario.",
            }
            case.ladder_step += 1
            _write_action_log(
                case=case,
                action_type="discount_pending_approval",
                outcome="pending_approval",
                reason="Discount would be attached but payment failures must not auto-discount. Awaiting merchant approval.",
                guardrail_checks=guardrails,
                db=db,
                amount_offered=session.cart_value,
                requires_human_approval=True,
            )
            db.commit()
            logger.warning(
                "Case %s: unexpected discount_allowed=True on payment failure — "
                "blocked pending human approval.", case.id
            )
            return case

        # Create the recovery payment link
        reference_id = f"pf_{session.event_id}_{case.ladder_step}_{uuid.uuid4().hex[:6]}"
        link_result = razorpay_client.create_recovery_payment_link(
            amount_rupees=session.cart_value,     # exact original amount — no discount
            customer_name=session.customer_name or session.customer_email,
            customer_email=session.customer_email,
            customer_phone=session.customer_phone,
            description=(
                "Payment retry — complete your purchase"
                if failure_class == "retryable_technical"
                else "Complete your purchase using a different payment method"
            ),
            reference_id=reference_id,
        )

        guardrails = {
            "contact_touches_before": case.contact_touches,
            "circuit_breaker_limit": CONTACT_TOUCH_LIMIT,
            "discount_allowed": False,
            "amount_offered_equals_at_risk": True,  # always true here
            "razorpay_reference_id": reference_id,
        }

        if not link_result.success:
            # Link creation failed — log the failure but don't crash the case
            case.ladder_step += 1
            case.last_action_at = now
            _write_action_log(
                case=case,
                action_type="payment_link_failed",
                outcome="failed",
                reason=f"Razorpay link creation failed: {link_result.error_message} ({link_result.error_type})",
                guardrail_checks=guardrails,
                db=db,
                amount_offered=session.cart_value,
            )
            db.commit()
            logger.error(
                "Case %s: payment link creation failed — %s (%s).",
                case.id, link_result.error_message, link_result.error_type,
            )
            return case

        # Success — update case state
        link_url = link_result.short_url or f"https://rzp.io/i/{link_result.payment_link_id}"
        case.status = CaseStatus.INTERVENING
        case.contact_touches += 1
        case.last_action_at = now
        case.ladder_step += 1

        log_result = _write_action_log(
            case=case,
            action_type="payment_link_sent",
            outcome="sent",
            reason=classification.description,
            guardrail_checks=guardrails,
            db=db,
            amount_offered=session.cart_value,
            coupon_code=None,   # never
        )

        if not log_result:
            # Idempotency hit: another thread/request already completed this exact
            # ladder step and wrote the log row. Abort before sending a duplicate email.
            return case

        # Resolve merchant for notification context
        merchant = _merchant_for_session(session, db)

        db.commit()  # commit before sending notifications (idempotency boundary)

        # Send notification (fire-and-forget — failures don't roll back the case)
        _send_payment_link_notification(session, case, link_url, merchant, db)

        logger.info(
            "Case %s: payment link sent (touch %d/%d) — %s",
            case.id, case.contact_touches, CONTACT_TOUCH_LIMIT, link_url,
        )
        return case

    # ── BRANCH D: unknown failure_class → escalate ────────────────────────────
    guardrails = {
        "failure_class": failure_class,
        "reason": "Unhandled failure class — escalating to human.",
    }
    case.status = CaseStatus.ESCALATED
    case.escalated_to_human = True
    case.escalation_reason = f"unhandled_class:{failure_class}"
    case.last_action_at = now
    case.ladder_step += 1

    _write_action_log(
        case=case,
        action_type="human_escalation",
        outcome="sent",
        reason=f"Unhandled failure_class={failure_class!r}. Escalating to human.",
        guardrail_checks=guardrails,
        db=db,
        requires_human_approval=False,
    )
    db.commit()
    logger.warning("Case %s: unhandled failure_class=%r — escalated.", case.id, failure_class)
    return case
