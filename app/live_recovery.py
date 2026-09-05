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
from app.db_models import (
    CheckoutSession, RecoveryOutcomeRecord, SessionStatus,
    RecoveryCase, RecoveryScenario, CaseStatus, ClassificationMethod,
)
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

    # Evaluate Part 5: Bounded auto-call for high-priority abandonments
    import json as _json
    from app.db_models import Product, MerchantUser, Coupon
    from app.voice_service import make_recovery_call
    from app.sms_service import send_recovery_sms
    from app.email_service import send_recovery_email
    import uuid
    
    from app.config import get_base_url
    base_url = get_base_url()

    product_data = []
    if session.cart_json:
        try:
            import json
            raw_items = json.loads(session.cart_json)
            for ri in raw_items:
                p = db.query(Product).filter(Product.id == ri.get("product_id")).first()
                if p:
                    product_data.append({
                        "name": p.name,
                        "price": p.price,
                        "quantity": ri.get("quantity", 1)
                    })
        except Exception:
            pass
            
    action_value_str = action.value
    if product_data or session.cart_json:
        # Fallback to get merchant if product lookup failed
        merchant_id = None
        if product_data:
            product = db.query(Product).filter(Product.name == product_data[0]["name"]).first()
            if product: merchant_id = product.merchant_id
        else:
            try:
                import json
                raw = json.loads(session.cart_json)
                p = db.query(Product).filter(Product.id == raw[0].get("product_id")).first()
                if p: merchant_id = p.merchant_id
            except: pass
            
        merchant = db.query(MerchantUser).filter(MerchantUser.id == merchant_id).first() if merchant_id else None
        
        if session.is_high_priority and merchant:
                # Guardrail: Only send the VIP discount if the diagnosed reason actually permits a discount.
                # Technical-failure sessions must receive only a plain retry link, even if high priority.
                from app.models import RecoveryAction
                
                if action == RecoveryAction.SEND_DISCOUNT_NUDGE:
                    # Generate a one-time 15% VIP coupon
                    vip_code = f"VIP{uuid.uuid4().hex[:6].upper()}"
                    vip_coupon = Coupon(
                        merchant_id=merchant.id,
                        code=vip_code,
                        discount_pct=15,
                        active=True,
                        usage_limit=1
                    )
                    db.add(vip_coupon)
                    db.commit()
                    
                    resume_url = outcome.short_url or f"{base_url}/cart?resume={session.event_id}&coupon={vip_code}"
                    
                    if merchant.auto_email_high_priority:
                        context = {
                            "store_name": merchant.store_name or "Our Store",
                            "customer_name": session.customer_name or "VIP Customer",
                            "cart_items": product_data,
                            "subtotal": session.cart_value,
                            "discount_amount": session.cart_value * 0.15,
                            "coupon_code": vip_code,
                            "total": session.cart_value * 0.85,
                            "resume_url": resume_url,
                            "message": "We noticed you left a high-value order in your cart. As a VIP, here's a special 15% discount just for you."
                        }
                        send_recovery_email(session.customer_email, "Your VIP Discount is inside!", "emails/recovery_email.html", context)
                        try:
                            from app.notification_service import send_in_app_notification
                            send_in_app_notification(db, session.customer_user_id, "VIP Discount inside! 🎁", f"We noticed you left a high-value order in your cart. Use code {vip_code} for 15% off!", resume_url)
                        except Exception as e:
                            logger.error(f"Failed to send in-app notif: {e}")
                        action_value_str = "auto_email_high_priority"
                        outcome.action_success = True
                    
                    if merchant.auto_call_high_priority:
                        # Do an auto call and SMS — capped to 1 automatic attempt
                        call_res = make_recovery_call(
                            session.customer_phone,
                            session.customer_name or "there",
                            session.cart_value * 0.85,
                            resume_url,
                            coupon_code=vip_code
                        )
                        sms_msg = f"Hi {session.customer_name or 'there'}, complete your order with 15% off using code {vip_code} at {resume_url}"
                        sms_res = send_recovery_sms(session.customer_phone, sms_msg)
                        
                        call_success = call_res.get("status") == "initiated"
                        sms_success = sms_res.get("status") == "sent"
                        outcome.action_success = call_success or sms_success
                        
                    action_value_str = "high_priority_vip_outreach"
                    outcome.amount_offered = session.cart_value * 0.85
                else:
                    # Reason does not permit discount (e.g. technical failure). 
                    # Send plain high priority reminder without discount.
                    resume_url = outcome.short_url or f"{base_url}/cart?resume={session.event_id}"
                    
                    if merchant.auto_email_high_priority:
                        context = {
                            "store_name": merchant.store_name or "Our Store",
                            "customer_name": session.customer_name or "VIP Customer",
                            "cart_items": product_data,
                            "subtotal": session.cart_value,
                            "discount_amount": 0,
                            "coupon_code": None,
                            "total": session.cart_value,
                            "resume_url": resume_url,
                            "message": "We noticed you left a high-value order in your cart. Please return to complete your purchase."
                        }
                        send_recovery_email(session.customer_email, "Complete your purchase", "emails/recovery_email.html", context)
                        action_value_str = "auto_email_high_priority"
                        outcome.action_success = True
                    
                    if merchant.auto_call_high_priority:
                        call_res = make_recovery_call(
                            session.customer_phone,
                            session.customer_name or "there",
                            session.cart_value,
                            resume_url
                        )
                        sms_msg = f"Hi {session.customer_name or 'there'}, complete your order at {resume_url}"
                        sms_res = send_recovery_sms(session.customer_phone, sms_msg)
                        
                        call_success = call_res.get("status") == "initiated"
                        sms_success = sms_res.get("status") == "sent"
                        outcome.action_success = call_success or sms_success
                        action_value_str = "auto_call"
        else:
            # Normal priority or merchant doesn't have auto_call_high_priority
            if outcome.action_success:
                resume_url = outcome.short_url or f"{base_url}/cart?resume={session.event_id}"
                
                discount_amount = session.cart_value - (outcome.amount_offered or session.cart_value)
                context = {
                    "store_name": merchant.store_name or "Our Store",
                    "customer_name": session.customer_name or "Customer",
                    "cart_items": product_data,
                    "subtotal": session.cart_value,
                    "discount_amount": discount_amount if discount_amount > 0 else 0,
                    "coupon_code": None, # automated discount is applied on the Razorpay link
                    "total": outcome.amount_offered or session.cart_value,
                    "resume_url": resume_url,
                    "message": merchant.automated_recovery_message or "Complete your checkout and save!"
                }
                send_recovery_email(session.customer_email, "Complete your purchase", "emails/recovery_email.html", context)
                try:
                    from app.notification_service import send_in_app_notification
                    notif_msg = merchant.automated_recovery_message or "Complete your checkout and save!"
                    send_in_app_notification(db, session.customer_user_id, "Complete your purchase", notif_msg, resume_url)
                except Exception as e:
                    logger.error(f"Failed to send in-app notif: {e}")

    record = RecoveryOutcomeRecord(
        session_id=session.id,
        predicted_reason=classification.predicted_reason.value,
        confidence=classification.confidence,
        classification_method=classification.method_used,
        reasoning=classification.reasoning,
        action_taken=action_value_str,
        action_success=outcome.action_success,
        amount_offered=outcome.amount_offered,
        confirmed_recovered_amount=outcome.confirmed_recovered_amount,
        payment_link_id=outcome.payment_link_id,
        error_message=outcome.error_message,
    )
    db.add(record)

    # ── Create/update the CHECKOUT_ABANDONMENT RecoveryCase ───────────────────
    # This is the NEW addition for the revenue dashboard.  It runs in the SAME
    # db.commit() call as the RecoveryOutcomeRecord above, so both land atomically.
    # The existing RecoveryOutcomeRecord schema and the rest of this function are
    # completely unchanged.
    _upsert_abandonment_recovery_case(session, classification, outcome, db)

    session.previous_recovery_attempts += 1
    db.commit()
    db.refresh(record)

    return record


def _upsert_abandonment_recovery_case(session, classification, outcome, db):
    """Creates (or silently skips if already exists) a RecoveryCase row tagged
    scenario=CHECKOUT_ABANDONMENT for this session.

    Called exclusively from run_recovery_for_session — never from the payment-failure
    pipeline.  The two pipelines write to different scenario buckets, guaranteeing
    they never share a case row.

    Classification metadata is mapped from the abandonment ClassificationResult so
    the dashboard can filter/sort by predicted reason and confidence without joining
    RecoveryOutcomeRecord."""
    existing = (
        db.query(RecoveryCase)
        .filter(
            RecoveryCase.checkout_session_id == session.id,
            RecoveryCase.scenario == RecoveryScenario.CHECKOUT_ABANDONMENT,
        )
        .first()
    )
    if existing:
        return  # already exists (e.g. abandon endpoint called twice) — skip

    # Resolve merchant from cart
    merchant_id = 1  # safe fallback for demo store
    _merchant_resolved = False
    try:
        import json as _j
        from app.db_models import Product
        if session.cart_json:
            raw = _j.loads(session.cart_json)
            if raw:
                p = db.query(Product).filter(Product.id == raw[0].get("product_id")).first()
                if p:
                    merchant_id = p.merchant_id
                    _merchant_resolved = True
    except Exception:
        pass

    if not _merchant_resolved:
        import logging as _log
        _log.getLogger("recovery_agent.live_recovery").warning(
            "merchant_id resolution failed for session %s (cart_json=%r) — "
            "falling back to merchant_id=1. This case will be orphaned and "
            "invisible to the real merchant's dashboard. Fix cart/product linkage.",
            session.id, session.cart_json,
        )

    # RAR score: rough proxy matching the payment-failure ladder's logic
    _rar = {
        "card_declined": 80.0,
        "network_drop": 70.0,
        "high_amount_hesitation": 90.0,
        "price_shock_at_checkout": 85.0,
        "otp_timeout": 60.0,
        "page_load_slow": 55.0,
        "accidental_close": 30.0,
        "unknown": 40.0,
    }
    rar_score = _rar.get(classification.predicted_reason.value, 40.0)
    if session.cart_value >= 5000:
        rar_score = min(100.0, rar_score + 20.0)

    # Map action outcome to CaseStatus
    if outcome.confirmed_recovered_amount:
        status = CaseStatus.RECOVERED
    elif outcome.action_success:
        status = CaseStatus.INTERVENING
    else:
        status = CaseStatus.AT_RISK

    case = RecoveryCase(
        merchant_id=merchant_id,
        customer_user_id=session.customer_user_id,
        checkout_session_id=session.id,
        scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
        amount_at_risk=session.cart_value,
        amount_recovered=outcome.confirmed_recovered_amount or 0.0,
        status=status,
        ladder_step=1,  # one ladder step attempted by the abandonment pipeline
        classification=classification.predicted_reason.value,
        classification_source=(
            ClassificationMethod.RULE if classification.method_used == "rule"
            else ClassificationMethod.LLM
        ),
        error_source=None,   # not applicable for abandonment (no gateway error)
        rar_score=rar_score,
        confidence=classification.confidence,
        escalated_to_human=False,
        contact_touches=1 if outcome.action_success else 0,
    )
    db.add(case)