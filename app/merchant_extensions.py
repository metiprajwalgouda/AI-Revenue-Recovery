import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta

import json
from app.db import get_db
from app.config import get_base_url
from app.db_models import (
    MerchantUser, CustomerUser, CheckoutSession, SessionStatus, RecoveryOutcomeRecord, Coupon,
    RecoveryCase, RecoveryActionLog, RecoveryScenario, CaseStatus,
    Invoice, InvoiceStatus, Product,
)
import logging
from app.merchant_auth_routes import get_current_merchant
from app.razorpay_client import get_razorpay_client
from app.whatsapp_service import send_whatsapp_message
from app.email_service import send_recovery_email
from app.sms_service import send_recovery_sms
from app.voice_service import make_recovery_call
from app.live_analytics import compute_confirmed_discounts
from pydantic import BaseModel

logger = logging.getLogger("recovery_agent.merchant_extensions")

router = APIRouter(prefix="/api/merchant")

class ManualRecoveryRequest(BaseModel):
    session_id: Optional[int] = None
    case_id: Optional[int] = None
    channel: str # email, sms, whatsapp, call
    discount_amount: Optional[float] = None
    discount_pct: Optional[int] = None
    coupon_id: Optional[int] = None
    custom_message: Optional[str] = None

@router.post("/manual-recovery")
def manual_recovery(
    payload: ManualRecoveryRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    session = None
    case = None

    if payload.session_id:
        session = db.query(CheckoutSession).filter(CheckoutSession.id == payload.session_id).first()
        if session:
            case = db.query(RecoveryCase).filter(RecoveryCase.checkout_session_id == session.id).first()

    if not session and payload.case_id:
        case = db.query(RecoveryCase).filter(RecoveryCase.id == payload.case_id).first()
        if case and case.checkout_session:
            session = case.checkout_session

    if not session and not case:
        if payload.session_id:
            # Fallback: check if session_id was actually a case_id
            case = db.query(RecoveryCase).filter(RecoveryCase.id == payload.session_id).first()
            if case and case.checkout_session:
                session = case.checkout_session

    if not session and not case:
        raise HTTPException(status_code=404, detail="Session or recovery case not found")
        
    outcome = None
    if session:
        outcome = db.query(RecoveryOutcomeRecord).filter(RecoveryOutcomeRecord.session_id == session.id).first()
        if not outcome:
            outcome = RecoveryOutcomeRecord(
                session_id=session.id,
                predicted_reason="unknown",
                confidence=1.0,
                classification_method="manual",
                reasoning="Manual merchant intervention",
                action_taken=payload.channel,
                action_success=False
            )
            db.add(outcome)
            db.flush()

    predicted_reason = outcome.predicted_reason if outcome else (case.classification if case else "manual")

    # Guardrails check
    restricted_reasons = ['bank_gateway_failure', 'otp_timeout', 'card_declined', 'unknown', 'manual', 'risk_terminal']
    if predicted_reason in restricted_reasons:
        payload.coupon_id = None
        payload.discount_pct = None
        payload.discount_amount = None

    from app.agent.recovery_actions import is_discount_allowed
    
    cart_value = session.cart_value if session else (case.amount_at_risk if case else 0.0)
    customer_phone = (session.customer_phone if session else None) or (case.customer.phone if case and case.customer else None)
    customer_name = (session.customer_name if session else None) or (case.customer.name if case and case.customer else "Customer")
    customer_email = (session.customer_email if session else None) or (case.customer.email if case and case.customer else None)
    event_id = session.event_id if session else (f"case_{case.id}" if case else "recovery")

    amount_offered = cart_value
    coupon = None
    discount_allowed = is_discount_allowed(predicted_reason)
    
    if discount_allowed:
        if payload.coupon_id:
            coupon = db.query(Coupon).filter(Coupon.id == payload.coupon_id, Coupon.merchant_id == current_merchant.id).first()
            if not coupon:
                raise HTTPException(status_code=400, detail="Coupon not found")
            if not coupon.active:
                raise HTTPException(status_code=400, detail="Coupon is inactive")
            if coupon.usage_limit is not None and coupon.times_used >= coupon.usage_limit:
                raise HTTPException(status_code=400, detail="Coupon usage limit reached")
            
            if coupon.discount_pct:
                amount_offered = cart_value * (1 - (coupon.discount_pct / 100.0))
            elif coupon.discount_amount:
                amount_offered = max(0.0, cart_value - coupon.discount_amount)
        elif payload.discount_pct:
            capped_pct = min(payload.discount_pct, current_merchant.max_discount_pct)
            amount_offered = cart_value * (1 - (capped_pct / 100.0))
        elif payload.discount_amount:
            max_allowed_amt = cart_value * (current_merchant.max_discount_pct / 100.0)
            capped_amt = min(payload.discount_amount, max_allowed_amt)
            amount_offered = max(0.0, cart_value - capped_amt)
    
    if outcome:
        outcome.amount_offered = amount_offered
    
    base_url = get_base_url(request)
    resume_url = f"{base_url}/cart?resume={event_id}"
    if payload.coupon_id and coupon:
        resume_url += f"&coupon={coupon.code}"
    
    base_message = payload.custom_message if payload.custom_message else "Complete your checkout and save!"
    if coupon:
        message = f"{base_message} Use code {coupon.code} at checkout. Resume here: {resume_url}"
    elif amount_offered < cart_value:
        message = f"{base_message} We've applied a discount for you. Resume here: {resume_url}"
    else:
        message = f"{base_message} Resume here: {resume_url}"
        
    error_message = None
    success = False

    if payload.channel == "whatsapp":
        if not customer_phone:
            raise HTTPException(status_code=400, detail="No customer phone number available")
        result = send_whatsapp_message(customer_phone, message)
        success = result.get("status") == "initiated"
        if not success:
            error_message = result.get("error", "Unknown WhatsApp error")
    elif payload.channel == "call":
        if not customer_phone:
            raise HTTPException(status_code=400, detail="No customer phone number available")
        result = make_recovery_call(customer_phone, customer_name, amount_offered, resume_url, coupon_code=coupon.code if coupon else None)
        success = result.get("status") in ("initiated", "skipped_no_key")
        if result.get("status") == "failed":
            error_message = result.get("error", "Unknown Call error")
    elif payload.channel == "sms":
        if not customer_phone:
            raise HTTPException(status_code=400, detail="No customer phone number available")
        result = send_recovery_sms(customer_phone, message)
        success = result.get("status") in ("sent", "initiated")
        if not success:
            error_message = result.get("error", "Unknown SMS error")
    elif payload.channel == "email":
        if not customer_email:
            raise HTTPException(status_code=400, detail="No customer email available")
        cart_items_data = []
        try:
            raw_items = json.loads(session.cart_json) if (session and session.cart_json) else []
            from app.db_models import Product
            for ri in raw_items:
                product = db.query(Product).filter(Product.id == ri.get('product_id')).first()
                if product:
                    cart_items_data.append({
                        "name": product.name,
                        "price": product.price,
                        "quantity": ri.get('quantity', 1)
                    })
        except Exception:
            pass

        discount_amount = cart_value - amount_offered
        context = {
            "store_name": current_merchant.store_name or "Our Store",
            "customer_name": customer_name,
            "cart_items": cart_items_data,
            "subtotal": cart_value,
            "discount_amount": discount_amount if discount_amount > 0 else 0,
            "coupon_code": coupon.code if coupon else None,
            "total": amount_offered,
            "resume_url": resume_url,
            "message": payload.custom_message
        }
        result = send_recovery_email(customer_email, "Special Recovery Offer", "emails/recovery_email.html", context)
        success = bool(result)
        if not success:
            error_message = "Failed to send email"
    
    if outcome:
        outcome.action_success = success
        outcome.delivery_status = "sent" if success else "failed"

    # Write RecoveryActionLog if case exists (or create one for tracking)
    if case:
        step = (case.ladder_step or 0) + 1
        case.ladder_step = step
        idempotency_key = f"{case.id}:{step}:manual_{payload.channel}_{uuid.uuid4().hex[:6]}"
        
        guardrails = {
            "channel": payload.channel,
            "manual_intervention": True,
            "discount_allowed": discount_allowed,
            "amount_offered": amount_offered,
            "original_amount": cart_value,
            "human_approved": True,
        }
        
        action_log = RecoveryActionLog(
            case_id=case.id,
            idempotency_key=idempotency_key,
            ladder_step=step,
            action_type=f"manual_{payload.channel}",
            reason=f"Manual merchant intervention ({payload.channel}): {payload.custom_message or 'Recovery offer'}",
            guardrail_checks=json.dumps(guardrails),
            outcome="sent" if success else "failed",
            amount_offered=amount_offered,
            coupon_code=coupon.code if coupon else None,
            requires_human_approval=False, # Skipped because merchant manually clicked send
            approved_by=current_merchant.id,
            approved_at=datetime.now(timezone.utc),
        )
        db.add(action_log)
        
        case.escalated_to_human = False
        case.status = CaseStatus.INTERVENING
        case.contact_touches = (case.contact_touches or 0) + 1
        case.last_action_at = datetime.now(timezone.utc)

    db.commit()
    
    if not success and error_message:
        return {"status": "failed", "success": False, "channel": payload.channel, "error": error_message}
    return {"status": "success", "success": True}

@router.get("/automated-actions")
def get_automated_actions(
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    # Fetch outcomes that were not manually triggered
    outcomes = db.query(RecoveryOutcomeRecord).join(CheckoutSession).filter(
        RecoveryOutcomeRecord.action_taken != "no_action",
        RecoveryOutcomeRecord.action_taken.notlike("manual_%")
    ).order_by(RecoveryOutcomeRecord.created_at.desc()).limit(10).all()
    
    result = []
    for o in outcomes:
        session = db.query(CheckoutSession).filter(CheckoutSession.id == o.session_id).first()
        customer_name = session.customer_name or session.customer_email or "Customer"
        amount = session.cart_value
        result.append({
            "customer_name": customer_name,
            "cart_value": amount,
            "action_taken": o.action_taken,
            "amount_offered": o.amount_offered,
            "timestamp": o.created_at.isoformat() if o.created_at else None
        })
    return result

from sqlalchemy import func
from app.db_models import CustomerUser, CheckoutSession, SessionStatus

@router.get("/customers")
def get_customers(db: Session = Depends(get_db), current_merchant: MerchantUser = Depends(get_current_merchant)):
    customers = db.query(CustomerUser).all()
    result = []
    for c in customers:
        # Get sessions for this customer
        sessions = db.query(CheckoutSession).filter(
            (CheckoutSession.customer_user_id == c.id) | (CheckoutSession.customer_email == c.email)
        ).all()
        completed = [s for s in sessions if s.status == SessionStatus.COMPLETED]
        last_order = max([s.started_at for s in sessions]) if sessions else None
        result.append({
            "id": c.id,
            "name": c.name or "Guest",
            "email": c.email,
            "phone": c.phone,
            "total_orders": len(completed),
            "total_spend": sum([s.cart_value for s in completed]),
            "total_sessions": len(sessions),
            "last_order_date": last_order.isoformat() if last_order else None,
            "opted_out_of_marketing": c.opted_out_of_marketing
        })
    return result

@router.get("/customers/{customer_id}")
def get_customer_details(customer_id: int, db: Session = Depends(get_db), current_merchant: MerchantUser = Depends(get_current_merchant)):
    customer = db.query(CustomerUser).filter(CustomerUser.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
        
    sessions = db.query(CheckoutSession).filter(
        (CheckoutSession.customer_user_id == customer.id) | (CheckoutSession.customer_email == customer.email)
    ).order_by(CheckoutSession.started_at.desc()).all()
    completed_sessions = [s for s in sessions if s.status == SessionStatus.COMPLETED]
    abandoned_sessions = [s for s in sessions if s.status in [SessionStatus.ABANDONED, SessionStatus.STARTED]]
    
    # Hydrate sessions
    session_list = []
    for s in sessions:
        items = []
        if s.cart_json:
            try:
                raw_items = json.loads(s.cart_json)
                for item in raw_items:
                    product = db.query(Product).filter(Product.id == item.get("product_id")).first()
                    items.append({
                        "product_id": item.get("product_id"),
                        "name": product.name if product else (item.get("name") or f"Product #{item.get('product_id')}"),
                        "quantity": item.get("quantity", 1),
                        "price": product.price if product else item.get("price", 0.0)
                    })
            except Exception:
                pass
        
        status_val = s.status.value if hasattr(s.status, "value") else str(s.status)
        session_list.append({
            "id": s.id,
            "event_id": s.event_id,
            "status": status_val,
            "cart_value": s.cart_value,
            "final_amount_charged": s.final_amount_charged,
            "items": items,
            "payment_id": s.razorpay_payment_id,
            "order_id": s.razorpay_order_id,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "recovered_from_session_id": s.recovered_from_session_id
        })
        
    # Recovery cases for this customer
    session_ids = [s.id for s in sessions]
    cases = db.query(RecoveryCase).filter(
        (RecoveryCase.customer_user_id == customer.id) | (RecoveryCase.checkout_session_id.in_(session_ids) if session_ids else False)
    ).order_by(RecoveryCase.created_at.desc()).all()
    
    case_list = []
    for c in cases:
        scenario_val = c.scenario.value if hasattr(c.scenario, "scenario") or hasattr(c.scenario, "value") else str(c.scenario)
        status_val = c.status.value if hasattr(c.status, "value") else str(c.status)
        
        logs = db.query(RecoveryActionLog).filter(RecoveryActionLog.case_id == c.id).order_by(RecoveryActionLog.created_at.asc()).all()
        log_list = [{
            "id": l.id,
            "action_type": l.action_type,
            "ladder_step": l.ladder_step,
            "reason": l.reason,
            "outcome": l.outcome,
            "amount_offered": l.amount_offered,
            "coupon_code": l.coupon_code,
            "created_at": l.created_at.isoformat() if l.created_at else None
        } for l in logs]
        
        case_list.append({
            "id": c.id,
            "scenario": scenario_val,
            "status": status_val,
            "amount_at_risk": c.amount_at_risk,
            "amount_recovered": c.amount_recovered,
            "coupon_code_used": c.coupon_code_used,
            "discount_amount": c.discount_amount,
            "classification": c.classification,
            "ladder_step": c.ladder_step,
            "escalated_to_human": c.escalated_to_human,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "last_action_at": c.last_action_at.isoformat() if c.last_action_at else None,
            "action_logs": log_list
        })
        
    # Invoices for this customer
    invoices = db.query(Invoice).filter(Invoice.customer_user_id == customer.id).order_by(Invoice.created_at.desc()).all()
    invoice_list = [{
        "id": inv.id,
        "invoice_number": inv.invoice_number,
        "amount": inv.amount,
        "status": inv.status.value if hasattr(inv.status, "value") else str(inv.status),
        "due_date": inv.due_date.isoformat() if inv.due_date else None,
        "paid_at": inv.paid_at.isoformat() if getattr(inv, "paid_at", None) else None,
        "created_at": inv.created_at.isoformat() if inv.created_at else None
    } for inv in invoices]
    
    return {
        "customer": {
            "id": customer.id,
            "name": customer.name or "Guest Customer",
            "email": customer.email,
            "phone": customer.phone or "N/A",
            "opted_out_of_marketing": customer.opted_out_of_marketing,
            "is_active": customer.is_active,
            "total_orders": len(completed_sessions),
            "total_spend": sum([s.cart_value for s in completed_sessions]),
            "total_sessions": len(sessions),
            "abandoned_sessions": len(abandoned_sessions),
            "created_at": customer.created_at.isoformat() if hasattr(customer, 'created_at') and customer.created_at else None
        },
        "sessions": session_list,
        "recovery_cases": case_list,
        "invoices": invoice_list
    }

class BroadcastRequest(BaseModel):
    customer_ids: list[int]
    coupon_id: Optional[int] = None
    message: Optional[str] = None

@router.post("/broadcast")
def send_broadcast(
    payload: BroadcastRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    sent = 0
    skipped = 0
    
    coupon = None
    if payload.coupon_id:
        coupon = db.query(Coupon).filter(Coupon.id == payload.coupon_id, Coupon.merchant_id == current_merchant.id).first()
        
    base_url = get_base_url(request)
    for cid in payload.customer_ids:
        customer = db.query(CustomerUser).filter(CustomerUser.id == cid).first()
        if not customer:
            continue
            
        if customer.opted_out_of_marketing:
            skipped += 1
            continue
            
        context = {
            "store_name": current_merchant.store_name or "Our Store",
            "customer_name": customer.name or "Customer",
            "cart_items": [],
            "subtotal": 0.0,
            "discount_amount": 0.0,
            "coupon_code": coupon.code if coupon else None,
            "total": 0.0,
            "resume_url": f"{base_url}/cart",
            "message": payload.message or "Here is a special offer for you!"
        }
        
        success = send_recovery_email(customer.email, "Special Offer", "emails/recovery_email.html", context)
        if success:
            sent += 1
            
    return {"status": "success", "sent": sent, "skipped": skipped}

@router.get("/advanced-metrics")
def get_advanced_metrics(db: Session = Depends(get_db), current_merchant: MerchantUser = Depends(get_current_merchant)):
    """Computes unified cross-scenario financial, funnel, cohort, and audit figures
    strictly from RecoveryCase, RecoveryActionLog, and Invoice for current_merchant.id.
    """
    cases = db.query(RecoveryCase).filter(
        RecoveryCase.merchant_id == current_merchant.id
    ).all()
    case_ids = [c.id for c in cases]
    case_map = {c.id: c for c in cases}

    action_logs = []
    if case_ids:
        action_logs = (
            db.query(RecoveryActionLog)
            .filter(RecoveryActionLog.case_id.in_(case_ids))
            .order_by(RecoveryActionLog.created_at.desc(), RecoveryActionLog.id.desc())
            .all()
        )

    # 1. Pipeline Funnel & Value across ALL 3 Scenarios
    total_cases = len(cases)
    total_at_risk_all = round(sum(c.amount_at_risk for c in cases if c.status not in (CaseStatus.RECOVERED, CaseStatus.LOST)), 2)
    total_detected_value = round(sum(c.amount_at_risk for c in cases), 2)  # Total value detected across pipeline
    total_recovered_all = round(sum(c.amount_recovered for c in cases), 2)
    total_lost_all = round(sum(c.amount_at_risk for c in cases if c.status == CaseStatus.LOST or str(c.status).upper() == "LOST"), 2)

    # Calculate confirmed discount cost strictly from recovered cases (realized redemptions)
    total_discount_cost = compute_confirmed_discounts(db, cases=cases)
    net_revenue_add = round(max(0.0, total_recovered_all - total_discount_cost), 2)
    cost_per_rupee = round(total_discount_cost / total_recovered_all, 4) if total_recovered_all > 0 else 0.0

    # Funnel counts
    suppressions = 0
    interventions = 0
    for l in action_logs:
        outcome_str = (l.outcome or "").lower()
        if outcome_str in ("suppressed", "blocked") or outcome_str.startswith("skipped"):
            suppressions += 1
        elif l.action_type not in ("flag_write_off_review",):
            interventions += 1

    successful_recoveries = sum(1 for c in cases if c.status == CaseStatus.RECOVERED or (c.amount_recovered and c.amount_recovered > 0))
    circuit_breaker_trips = sum(1 for c in cases if c.status == CaseStatus.LOST and (c.contact_touches or 0) >= 3)
    escalated_count = sum(1 for c in cases if c.escalated_to_human or c.status == CaseStatus.ESCALATED)

    # 2. Unified Cohorts (Group by diagnosis / classification)
    reason_breakdown = {}
    for c in cases:
        reason = c.classification or (c.scenario.value if hasattr(c.scenario, "value") else str(c.scenario))
        if not reason or reason == "None":
            reason = "unclassified"
        
        is_rec = (c.status == CaseStatus.RECOVERED or (c.amount_recovered and c.amount_recovered > 0))
        sc_val = c.scenario.value if hasattr(c.scenario, "value") else str(c.scenario)
        
        if reason not in reason_breakdown:
            reason_breakdown[reason] = {
                "count": 0,
                "recovered": 0,
                "value": 0.0,
                "at_risk_value": 0.0,
                "scenario": sc_val,
            }
        reason_breakdown[reason]["count"] += 1
        reason_breakdown[reason]["at_risk_value"] = round(reason_breakdown[reason]["at_risk_value"] + c.amount_at_risk, 2)
        if is_rec:
            reason_breakdown[reason]["recovered"] += 1
            reason_breakdown[reason]["value"] = round(reason_breakdown[reason]["value"] + c.amount_recovered, 2)

    # 3. Combined Audit Logs (direct from RecoveryActionLog)
    audit_logs_list = []
    for l in action_logs[:50]:
        c = case_map.get(l.case_id)
        if not c:
            continue
        sc_val = c.scenario.value if hasattr(c.scenario, "value") else str(c.scenario)
        is_rec = (l.action_type == "case_recovered" or c.status == CaseStatus.RECOVERED)
        
        outcome_str = (l.outcome or "").lower()
        if is_rec:
            status_flag = "Recovered"
        elif outcome_str in ("suppressed", "blocked") or outcome_str.startswith("skipped"):
            status_flag = "Blocked by Rule"
        else:
            status_flag = l.outcome.replace("_", " ").title() if l.outcome else "Pending"

        diag = c.classification or sc_val
        
        target_id = f"Case #{c.id}"
        if hasattr(c, "invoice") and c.invoice and c.invoice.invoice_number:
            target_id = f"{c.invoice.invoice_number}"
        elif hasattr(c, "checkout_session") and c.checkout_session and getattr(c.checkout_session, "event_id", None):
            target_id = f"{c.checkout_session.event_id}"
        elif c.checkout_session_id:
            target_id = f"Session #{c.checkout_session_id}"

        discount_cost = 0.0
        if c.discount_amount and is_rec and c.discount_amount > 0:
            discount_cost = float(c.discount_amount)
        elif l.amount_offered and l.amount_offered > 0 and c.amount_at_risk:
            if l.amount_offered < c.amount_at_risk:
                if l.action_type in ("issue_coupon", "discount_nudge") and l.amount_offered <= c.amount_at_risk * 0.5:
                    discount_cost = float(l.amount_offered)
                else:
                    discount_cost = float(c.amount_at_risk - l.amount_offered)

        impact = float(c.amount_recovered) if is_rec else 0.0

        # Parse guardrails
        guardrails_parsed = {}
        if l.guardrail_checks:
            try:
                guardrails_parsed = json.loads(l.guardrail_checks) if isinstance(l.guardrail_checks, str) else l.guardrail_checks
            except Exception:
                guardrails_parsed = {"raw": str(l.guardrail_checks)}

        cust_name = "Customer"
        if hasattr(c, "customer") and c.customer and c.customer.name:
            cust_name = c.customer.name
        elif hasattr(c, "checkout_session") and c.checkout_session and getattr(c.checkout_session, "customer_name", None):
            cust_name = c.checkout_session.customer_name

        audit_logs_list.append({
            "id": l.id,
            "timestamp": l.created_at.isoformat() if l.created_at else "",
            "case_id": c.id,
            "session_id": target_id,
            "scenario": sc_val,
            "diagnosis": diag,
            "action": l.action_type or "none",
            "reasoning": l.reason or "",
            "financial_impact": impact,
            "discount_cost": discount_cost,
            "status": status_flag,
            "outcome": l.outcome or "sent",
            "ladder_step": l.ladder_step,
            "amount_at_risk": float(c.amount_at_risk or 0.0),
            "amount_recovered": float(c.amount_recovered or 0.0),
            "customer_name": cust_name,
            "guardrail_checks": guardrails_parsed,
            "idempotency_key": l.idempotency_key or "",
            "approved_by": l.approved_by,
        })

    # 4. Per-scenario Breakdown
    def _compute_scenario_stats(sc_cases):
        tot_cases = len(sc_cases)
        at_risk = round(sum(c.amount_at_risk for c in sc_cases if c.status not in (CaseStatus.RECOVERED, CaseStatus.LOST)), 2)
        recovered = round(sum(c.amount_recovered for c in sc_cases), 2)
        lost = round(sum(c.amount_at_risk for c in sc_cases if c.status == CaseStatus.LOST or str(c.status).upper() == "LOST"), 2)
        lost_count = sum(1 for c in sc_cases if c.status == CaseStatus.LOST or str(c.status).upper() == "LOST")
        recovered_cases = sum(1 for c in sc_cases if c.status == CaseStatus.RECOVERED or (c.amount_recovered and c.amount_recovered > 0))
        recovery_rate = round((recovered_cases / tot_cases * 100.0), 2) if tot_cases > 0 else 0.0
        escalated = sum(1 for c in sc_cases if c.escalated_to_human or c.status == CaseStatus.ESCALATED)
        cb_trips = sum(1 for c in sc_cases if c.status == CaseStatus.LOST and (c.contact_touches or 0) >= 3)
        
        # Calculate DSO
        now_dt = datetime.now(timezone.utc)
        dso_list = []
        for c in sc_cases:
            issue_dt = None
            if hasattr(c, "invoice") and c.invoice and c.invoice.issue_date:
                issue_dt = c.invoice.issue_date.replace(tzinfo=timezone.utc) if c.invoice.issue_date.tzinfo is None else c.invoice.issue_date
            elif c.created_at:
                issue_dt = c.created_at.replace(tzinfo=timezone.utc) if c.created_at.tzinfo is None else c.created_at
            
            if issue_dt:
                if c.status == CaseStatus.RECOVERED or str(c.status).upper() == "RECOVERED":
                    end_dt = c.last_action_at or c.updated_at or now_dt
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
                    days = max(0.0, (end_dt - issue_dt).total_seconds() / 86400.0)
                    dso_list.append(days)
                elif c.status not in (CaseStatus.LOST, "lost", "LOST"):
                    days = max(0.0, (now_dt - issue_dt).total_seconds() / 86400.0)
                    dso_list.append(days)
        dso_days = round(sum(dso_list) / len(dso_list), 1) if dso_list else 0.0

        return {
            "at_risk": at_risk,
            "recovered": recovered,
            "lost": lost,
            "lost_count": lost_count,
            "recovery_rate": recovery_rate,
            "escalated": escalated,
            "circuit_breaker_trips": cb_trips,
            "dso_days": dso_days,
            "total_cases": tot_cases,
            "recovered_cases": recovered_cases,
        }

    pf_cases = [c for c in cases if c.scenario in (RecoveryScenario.PAYMENT_FAILURE, "payment_failure")]
    ab_cases = [c for c in cases if c.scenario in (RecoveryScenario.CHECKOUT_ABANDONMENT, "checkout_abandonment")]
    rec_cases = [c for c in cases if c.scenario in (RecoveryScenario.OVERDUE_RECEIVABLE, "overdue_receivable")]

    payment_failure_stats = _compute_scenario_stats(pf_cases)
    checkout_abandonment_stats = _compute_scenario_stats(ab_cases)
    overdue_receivable_stats = _compute_scenario_stats(rec_cases) if rec_cases else {
        "at_risk": 0.0,
        "recovered": 0.0,
        "lost": 0.0,
        "lost_count": 0,
        "recovery_rate": 0.0,
        "escalated": 0,
        "circuit_breaker_trips": 0,
        "dso_days": 0.0,
        "total_cases": 0,
        "recovered_cases": 0,
    }

    overall_stats = {
        "cost_per_rupee_recovered": cost_per_rupee,
        "total_at_risk": total_at_risk_all,
        "total_recovered": total_recovered_all,
        "total_lost": total_lost_all,
    }

    return {
        "payment_failure": payment_failure_stats,
        "checkout_abandonment": checkout_abandonment_stats,
        "overdue_receivable": overdue_receivable_stats,
        "overall": overall_stats,
        "funnel": {
            "total_detected": total_cases,
            "total_cases_detected": total_cases,
            "suppressed_by_guardrails": suppressions,
            "interventions_attempted": interventions,
            "successful_recoveries": successful_recoveries,
            "circuit_breaker_trips": circuit_breaker_trips,
            "escalated_to_human_count": escalated_count,
        },
        "financials": {
            "total_abandoned_value": total_detected_value,
            "total_at_risk": total_at_risk_all,
            "total_recovered": total_recovered_all,
            "total_lost": total_lost_all,
            "agent_recovered_revenue": total_recovered_all,
            "control_recovered_revenue": 0.0,
            "cost_of_discounts": total_discount_cost,
            "net_revenue_add": net_revenue_add,
            "cost_per_rupee_recovered": cost_per_rupee,
        },
        "cohorts": reason_breakdown,
        "audit_logs": audit_logs_list,
    }

@router.get("/live-sessions")
def get_live_sessions(
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    sessions = db.query(CheckoutSession).filter(CheckoutSession.status == SessionStatus.STARTED).order_by(CheckoutSession.started_at.desc()).all()
    return [{
        "id": s.id,
        "event_id": s.event_id,
        "customer_name": s.customer_name or "Guest",
        "customer_email": s.customer_email,
        "cart_value": s.cart_value,
        "started_at": s.started_at.isoformat()
    } for s in sessions]

class CouponCreate(BaseModel):
    code: str
    discount_pct: Optional[int] = None
    discount_amount: Optional[float] = None
    active: bool = True
    usage_limit: Optional[int] = None

@router.post("/coupons")
def create_coupon(payload: CouponCreate, db: Session = Depends(get_db), current_merchant: MerchantUser = Depends(get_current_merchant)):
    if payload.discount_pct and payload.discount_amount:
        raise HTTPException(status_code=400, detail="Cannot specify both percentage and flat amount discount")
    if not payload.discount_pct and not payload.discount_amount:
        raise HTTPException(status_code=400, detail="Must specify either percentage or flat amount discount")
        
    coupon = Coupon(**payload.model_dump(), merchant_id=current_merchant.id)
    db.add(coupon)
    db.commit()
    return {"status": "success", "id": coupon.id}

@router.get("/coupons")
def list_coupons(db: Session = Depends(get_db), current_merchant: MerchantUser = Depends(get_current_merchant)):
    coupons = db.query(Coupon).filter(Coupon.merchant_id == current_merchant.id).all()
    return [{"id": c.id, "code": c.code, "discount_pct": c.discount_pct, "discount_amount": c.discount_amount, "active": c.active, "usage_limit": c.usage_limit, "times_used": c.times_used} for c in coupons]

@router.get("/coupons/usage")
def list_coupon_usage(db: Session = Depends(get_db), current_merchant: MerchantUser = Depends(get_current_merchant)):
    from app.db_models import CouponUsageLog
    logs = db.query(CouponUsageLog).join(Coupon).filter(Coupon.merchant_id == current_merchant.id).order_by(CouponUsageLog.used_at.desc()).all()
    return [{
        "id": l.id,
        "coupon_code": l.coupon.code if l.coupon else "Unknown",
        "customer_email": l.customer_email,
        "used_at": l.used_at.isoformat(),
        "order_value": l.order_value,
        "discount_amount": l.discount_amount
    } for l in logs]

@router.put("/coupons/{coupon_id}")
def update_coupon(coupon_id: int, payload: CouponCreate, db: Session = Depends(get_db), current_merchant: MerchantUser = Depends(get_current_merchant)):
    if payload.discount_pct and payload.discount_amount:
        raise HTTPException(status_code=400, detail="Cannot specify both percentage and flat amount discount")
    if not payload.discount_pct and not payload.discount_amount:
        raise HTTPException(status_code=400, detail="Must specify either percentage or flat amount discount")
        
    coupon = db.query(Coupon).filter(Coupon.id == coupon_id, Coupon.merchant_id == current_merchant.id).first()
    if not coupon:
        raise HTTPException(404, "Coupon not found")
    coupon.code = payload.code
    coupon.discount_pct = payload.discount_pct
    coupon.discount_amount = payload.discount_amount
    coupon.active = payload.active
    coupon.usage_limit = payload.usage_limit
    db.commit()
    return {"status": "success"}


@router.get("/channels")
def get_channels():
    import os
    return {
        "email": bool(os.getenv("RESEND_API_KEY")),
        "sms": bool(os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN") and os.getenv("TWILIO_PHONE_NUMBER")),
        "call": bool(os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN") and os.getenv("TWILIO_PHONE_NUMBER")),
        "whatsapp": bool(os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN"))
    }

from pydantic import BaseModel
from typing import Optional

class InAppBroadcastRequest(BaseModel):
    title: str
    message: str
    action_url: Optional[str] = None

@router.post("/in-app-broadcast")
def in_app_broadcast_notification(payload: InAppBroadcastRequest, db: Session = Depends(get_db), current_merchant: MerchantUser = Depends(get_current_merchant)):
    from app.db_models import CustomerUser
    from app.notification_service import send_in_app_notification
    
    customers = db.query(CustomerUser).all()
    count = 0
    for c in customers:
        send_in_app_notification(db, c.id, payload.title, payload.message, payload.action_url)
        count += 1
        
    return {"status": "success", "sent_count": count}


# ---------------------------------------------------------------------------
# Payment Failures Dashboard & Audit Endpoints
# ---------------------------------------------------------------------------

@router.get("/payment-failures")
def get_merchant_payment_failures(
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    """Returns all payment failure recovery cases for this merchant with top summary metrics."""
    # Strict merchant filter — no cross-merchant leakage via OR merchant_id==1 fallback.
    cases = (
        db.query(RecoveryCase)
        .filter(
            RecoveryCase.scenario == RecoveryScenario.PAYMENT_FAILURE,
            RecoveryCase.merchant_id == current_merchant.id,
        )
        .order_by(RecoveryCase.created_at.desc())
        .all()
    )

    total_cases = len(cases)
    total_at_risk = sum(
        c.amount_at_risk for c in cases
        if c.status not in (CaseStatus.RECOVERED, CaseStatus.LOST)
    )
    total_recovered = sum(c.amount_recovered for c in cases)
    total_lost = sum(c.amount_at_risk for c in cases if c.status == CaseStatus.LOST or str(c.status).upper() == "LOST")
    lost_cases = sum(1 for c in cases if c.status == CaseStatus.LOST or str(c.status).upper() == "LOST")
    recovered_cases = sum(
        1 for c in cases
        if c.status == CaseStatus.RECOVERED or (c.amount_recovered and c.amount_recovered > 0)
    )
    recovery_rate = (recovered_cases / total_cases * 100.0) if total_cases > 0 else 0.0
    escalated_count = sum(1 for c in cases if c.escalated_to_human)
    circuit_breaker_trips = sum(
        1 for c in cases
        if c.status == CaseStatus.LOST and (c.contact_touches or 0) >= 3
    )

    cases_list = []
    for c in cases:
        customer_name = None
        customer_email = None
        if c.customer:
            customer_name = c.customer.name
            customer_email = c.customer.email
        elif c.checkout_session:
            customer_name = c.checkout_session.customer_name
            customer_email = c.checkout_session.customer_email

        cases_list.append({
            "id": c.id,
            "merchant_id": c.merchant_id,
            "customer_name": customer_name or "Guest Customer",
            "customer_email": customer_email or "N/A",
            "checkout_session_id": c.checkout_session_id,
            "amount_at_risk": c.amount_at_risk,
            "amount_recovered": c.amount_recovered,
            "status": c.status.value if hasattr(c.status, "value") else str(c.status),
            "ladder_step": c.ladder_step,
            "classification": c.classification,
            "classification_source": c.classification_source.value if hasattr(c.classification_source, "value") else str(c.classification_source) if c.classification_source else None,
            "error_source": c.error_source or "unknown",
            "rar_score": c.rar_score,
            "confidence": c.confidence,
            "escalated_to_human": c.escalated_to_human,
            "escalation_reason": c.escalation_reason,
            "contact_touches": c.contact_touches,
            "last_action_at": c.last_action_at.isoformat() if c.last_action_at else (c.updated_at.isoformat() if c.updated_at else (c.created_at.isoformat() if c.created_at else None)),
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })

    return {
        "summary": {
            "total_at_risk": round(total_at_risk, 2),
            "total_recovered": round(total_recovered, 2),
            "total_lost": round(total_lost, 2),
            "lost_cases": lost_cases,
            "recovery_rate_pct": round(recovery_rate, 1),
            "escalated_to_human_count": escalated_count,
            "circuit_breaker_trips": circuit_breaker_trips,
            "total_cases": total_cases,
            "recovered_cases": recovered_cases,
        },
        "cases": cases_list
    }


@router.get("/payment-failures/{case_id}/audit-trail")
def get_payment_failure_audit_trail(
    case_id: int,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    """Returns chronological audit logs for a specific recovery case."""
    case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
    if not case:
        raise HTTPException(status_code=404, detail="Recovery case not found")

    logs = (
        db.query(RecoveryActionLog)
        .filter(RecoveryActionLog.case_id == case_id)
        .order_by(RecoveryActionLog.created_at.asc())
        .all()
    )

    result = []
    for l in logs:
        guardrails = {}
        if l.guardrail_checks:
            try:
                guardrails = json.loads(l.guardrail_checks)
            except Exception:
                guardrails = {"raw": l.guardrail_checks}

        result.append({
            "id": l.id,
            "case_id": l.case_id,
            "ladder_step": l.ladder_step,
            "action_type": l.action_type,
            "reason": l.reason,
            "guardrail_checks": guardrails,
            "outcome": l.outcome,
            "amount_offered": l.amount_offered,
            "coupon_code": l.coupon_code,
            "requires_human_approval": l.requires_human_approval,
            "approved_by": l.approved_by,
            "approved_at": l.approved_at.isoformat() if l.approved_at else None,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        })

    return {
        "case_id": case.id,
        "scenario": case.scenario.value if hasattr(case.scenario, "value") else str(case.scenario),
        "status": case.status.value if hasattr(case.status, "value") else str(case.status),
        "amount_at_risk": case.amount_at_risk,
        "amount_recovered": case.amount_recovered,
        "logs": result
    }


# ---------------------------------------------------------------------------
# Checkout Abandonment Dashboard & Audit Endpoints
# ---------------------------------------------------------------------------

@router.get("/checkout-abandonment")
def get_merchant_checkout_abandonment(
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    """Returns all checkout abandonment recovery cases for this merchant with summary metrics."""
    cases = (
        db.query(RecoveryCase)
        .filter(
            RecoveryCase.scenario == RecoveryScenario.CHECKOUT_ABANDONMENT,
            RecoveryCase.merchant_id == current_merchant.id,
        )
        .order_by(RecoveryCase.created_at.desc())
        .all()
    )

    total_cases = len(cases)
    total_at_risk = sum(
        c.amount_at_risk for c in cases
        if c.status not in (CaseStatus.RECOVERED, CaseStatus.LOST)
    )
    total_recovered = sum(c.amount_recovered for c in cases)
    total_lost = sum(c.amount_at_risk for c in cases if c.status == CaseStatus.LOST or str(c.status).upper() == "LOST")
    lost_cases = sum(1 for c in cases if c.status == CaseStatus.LOST or str(c.status).upper() == "LOST")
    recovered_cases = sum(
        1 for c in cases
        if c.status == CaseStatus.RECOVERED or (c.amount_recovered and c.amount_recovered > 0)
    )
    recovery_rate = (recovered_cases / total_cases * 100.0) if total_cases > 0 else 0.0
    escalated_count = sum(1 for c in cases if c.escalated_to_human or c.status == CaseStatus.ESCALATED)
    circuit_breaker_trips = sum(
        1 for c in cases
        if c.status == CaseStatus.LOST and (c.contact_touches or 0) >= 3
    )

    cases_list = []
    for c in cases:
        customer_name = None
        customer_email = None
        if c.customer:
            customer_name = c.customer.name
            customer_email = c.customer.email
        elif hasattr(c, "checkout_session") and c.checkout_session:
            customer_name = c.checkout_session.customer_name
            customer_email = c.checkout_session.customer_email

        cases_list.append({
            "id": c.id,
            "merchant_id": c.merchant_id,
            "customer_name": customer_name or "Guest Customer",
            "customer_email": customer_email or "N/A",
            "checkout_session_id": c.checkout_session_id,
            "amount_at_risk": c.amount_at_risk,
            "amount_recovered": c.amount_recovered,
            "status": c.status.value if hasattr(c.status, "value") else str(c.status),
            "ladder_step": c.ladder_step,
            "classification": c.classification or "unclassified",
            "classification_source": c.classification_source.value if hasattr(c.classification_source, "value") else str(c.classification_source) if c.classification_source else None,
            "rar_score": c.rar_score,
            "confidence": c.confidence,
            "escalated_to_human": c.escalated_to_human,
            "escalation_reason": c.escalation_reason,
            "contact_touches": c.contact_touches,
            "last_action_at": c.last_action_at.isoformat() if c.last_action_at else (c.updated_at.isoformat() if c.updated_at else (c.created_at.isoformat() if c.created_at else None)),
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })

    return {
        "summary": {
            "total_at_risk": round(total_at_risk, 2),
            "total_recovered": round(total_recovered, 2),
            "total_lost": round(total_lost, 2),
            "lost_cases": lost_cases,
            "recovery_rate_pct": round(recovery_rate, 1),
            "escalated_to_human_count": escalated_count,
            "circuit_breaker_trips": circuit_breaker_trips,
            "total_cases": total_cases,
            "recovered_cases": recovered_cases,
        },
        "cases": cases_list
    }


@router.get("/checkout-abandonment/{case_id}/audit-trail")
def get_checkout_abandonment_audit_trail(
    case_id: int,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    """Returns chronological audit logs for a specific checkout abandonment recovery case."""
    case = db.query(RecoveryCase).filter(
        RecoveryCase.id == case_id,
        RecoveryCase.merchant_id == current_merchant.id,
    ).first()
    if not case:
        raise HTTPException(status_code=404, detail="Recovery case not found")

    logs = (
        db.query(RecoveryActionLog)
        .filter(RecoveryActionLog.case_id == case_id)
        .order_by(RecoveryActionLog.created_at.asc())
        .all()
    )

    result = []
    for l in logs:
        guardrails = {}
        if l.guardrail_checks:
            try:
                guardrails = json.loads(l.guardrail_checks)
            except Exception:
                guardrails = {"raw": l.guardrail_checks}

        result.append({
            "id": l.id,
            "case_id": l.case_id,
            "ladder_step": l.ladder_step,
            "action_type": l.action_type,
            "reason": l.reason,
            "guardrail_checks": guardrails,
            "outcome": l.outcome,
            "amount_offered": l.amount_offered,
            "coupon_code": l.coupon_code,
            "requires_human_approval": l.requires_human_approval,
            "approved_by": l.approved_by,
            "approved_at": l.approved_at.isoformat() if l.approved_at else None,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        })

    return {
        "case_id": case.id,
        "scenario": case.scenario.value if hasattr(case.scenario, "value") else str(case.scenario),
        "status": case.status.value if hasattr(case.status, "value") else str(case.status),
        "amount_at_risk": case.amount_at_risk,
        "amount_recovered": case.amount_recovered,
        "logs": result
    }


# ---------------------------------------------------------------------------
# Overdue Receivables Dashboard & Audit Endpoints
# ---------------------------------------------------------------------------

@router.get("/receivables")
def get_merchant_receivables(
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    """Returns all overdue receivable recovery cases for this merchant with summary metrics."""
    cases = (
        db.query(RecoveryCase)
        .filter(
            RecoveryCase.scenario == RecoveryScenario.OVERDUE_RECEIVABLE,
            RecoveryCase.merchant_id == current_merchant.id,
        )
        .order_by(RecoveryCase.created_at.desc())
        .all()
    )

    now = datetime.now(timezone.utc)
    total_cases = len(cases)
    total_at_risk = sum(
        c.amount_at_risk for c in cases
        if c.status not in (CaseStatus.RECOVERED, CaseStatus.LOST)
    )
    total_recovered = sum(c.amount_recovered for c in cases)
    total_lost = sum(c.amount_at_risk for c in cases if c.status == CaseStatus.LOST or str(c.status).upper() == "LOST" or (c.invoice and c.invoice.status == InvoiceStatus.WRITE_OFF_REVIEW))
    lost_cases = sum(1 for c in cases if c.status == CaseStatus.LOST or str(c.status).upper() == "LOST" or (c.invoice and c.invoice.status == InvoiceStatus.WRITE_OFF_REVIEW))
    recovered_cases = sum(
        1 for c in cases
        if c.status == CaseStatus.RECOVERED or (c.amount_recovered and c.amount_recovered > 0)
    )
    recovery_rate = (recovered_cases / total_cases * 100.0) if total_cases > 0 else 0.0
    escalated_count = sum(1 for c in cases if c.escalated_to_human or c.status == CaseStatus.ESCALATED)

    # DSO Calculation (average days from issue_date to either RECOVERED date or now for open cases)
    dso_durations = []
    for c in cases:
        issue_dt = None
        if c.invoice and c.invoice.issue_date:
            issue_dt = c.invoice.issue_date.replace(tzinfo=timezone.utc) if c.invoice.issue_date.tzinfo is None else c.invoice.issue_date
        elif c.created_at:
            issue_dt = c.created_at.replace(tzinfo=timezone.utc) if c.created_at.tzinfo is None else c.created_at

        if issue_dt:
            if c.status == CaseStatus.RECOVERED or str(c.status).upper() == "RECOVERED":
                end_dt = c.last_action_at or c.updated_at or now
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                days = max(0.0, (end_dt - issue_dt).total_seconds() / 86400.0)
                dso_durations.append(days)
            elif c.status not in (CaseStatus.LOST, "lost", "LOST"):
                days = max(0.0, (now - issue_dt).total_seconds() / 86400.0)
                dso_durations.append(days)

    avg_dso = round(sum(dso_durations) / len(dso_durations), 1) if dso_durations else 0.0

    cases_list = []
    for c in cases:
        customer_name = None
        customer_email = None
        if c.customer:
            customer_name = c.customer.name
            customer_email = c.customer.email
        elif c.invoice and c.invoice.customer:
            customer_name = c.invoice.customer.name
            customer_email = c.invoice.customer.email

        inv = c.invoice
        invoice_number = inv.invoice_number if inv else f"INV-CASE-{c.id}"
        inv_status = inv.status.value if (inv and hasattr(inv.status, "value")) else (str(inv.status) if inv else None)
        due_date_iso = inv.due_date.isoformat() if (inv and inv.due_date) else None
        issue_date_iso = inv.issue_date.isoformat() if (inv and inv.issue_date) else None

        days_overdue = 0
        is_overdue = False
        if inv and inv.due_date:
            due_dt = inv.due_date.replace(tzinfo=timezone.utc) if inv.due_date.tzinfo is None else inv.due_date
            diff = (now - due_dt).total_seconds() / 86400.0
            if diff > 0:
                is_overdue = True
                days_overdue = int(diff)
            else:
                days_overdue = 0

        cases_list.append({
            "id": c.id,
            "case_id": c.id,
            "merchant_id": c.merchant_id,
            "customer_name": customer_name or "Guest Customer",
            "customer_email": customer_email or "N/A",
            "invoice_id": c.invoice_id,
            "invoice_number": invoice_number,
            "invoice_status": inv_status,
            "amount_at_risk": c.amount_at_risk,
            "amount_recovered": c.amount_recovered,
            "status": c.status.value if hasattr(c.status, "value") else str(c.status),
            "ladder_step": c.ladder_step,
            "due_date": due_date_iso,
            "issue_date": issue_date_iso,
            "days_overdue": days_overdue,
            "is_overdue": is_overdue,
            "escalated_to_human": c.escalated_to_human,
            "escalation_reason": c.escalation_reason,
            "last_action_at": c.last_action_at.isoformat() if c.last_action_at else (c.updated_at.isoformat() if c.updated_at else (c.created_at.isoformat() if c.created_at else None)),
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })

    return {
        "summary": {
            "total_at_risk": round(total_at_risk, 2),
            "total_recovered": round(total_recovered, 2),
            "total_lost": round(total_lost, 2),
            "lost_cases": lost_cases,
            "recovery_rate_pct": round(recovery_rate, 1),
            "escalated_to_human_count": escalated_count,
            "dso_days": avg_dso,
            "total_cases": total_cases,
            "recovered_cases": recovered_cases,
        },
        "cases": cases_list
    }


@router.get("/receivables/{case_id}/audit-trail")
def get_receivable_audit_trail(
    case_id: int,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    """Returns chronological audit logs for a specific receivables recovery case.
    
    Strictly scoped to current_merchant.id.
    """
    case = db.query(RecoveryCase).filter(
        RecoveryCase.id == case_id,
        RecoveryCase.merchant_id == current_merchant.id
    ).first()
    if not case:
        raise HTTPException(status_code=404, detail="Recovery case not found")

    logs = (
        db.query(RecoveryActionLog)
        .filter(RecoveryActionLog.case_id == case_id)
        .order_by(RecoveryActionLog.created_at.asc())
        .all()
    )

    result = []
    for l in logs:
        guardrails = {}
        if l.guardrail_checks:
            try:
                guardrails = json.loads(l.guardrail_checks)
            except Exception:
                guardrails = {"raw": l.guardrail_checks}

        result.append({
            "id": l.id,
            "case_id": l.case_id,
            "ladder_step": l.ladder_step,
            "action_type": l.action_type,
            "reason": l.reason,
            "guardrail_checks": guardrails,
            "outcome": l.outcome,
            "amount_offered": l.amount_offered,
            "coupon_code": l.coupon_code,
            "requires_human_approval": l.requires_human_approval,
            "approved_by": l.approved_by,
            "approved_at": l.approved_at.isoformat() if l.approved_at else None,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        })

    return {
        "case_id": case.id,
        "scenario": case.scenario.value if hasattr(case.scenario, "value") else str(case.scenario),
        "status": case.status.value if hasattr(case.status, "value") else str(case.status),
        "amount_at_risk": case.amount_at_risk,
        "amount_recovered": case.amount_recovered,
        "invoice_number": case.invoice.invoice_number if case.invoice else None,
        "logs": result
    }


@router.post("/recovery-actions/{log_id}/approve")
def approve_recovery_action(
    log_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    """Merchant approves a pending_approval recovery action.

    Guards (in order):
      1. Log must exist.
      2. Log must be in outcome=pending_approval — rejects 400 if already
         approved/rejected/sent, preventing double-approval/double-send.
      3. The case must belong to this merchant — prevents cross-merchant approval.

    After approval:
      - Sets outcome="approved", approved_by, approved_at.
      - Advances case status to INTERVENING if still NEW/AT_RISK.
      - Fires the actual stored notification (payment link email + in-app) using
        the amount_offered / coupon_code stored on the log row at creation time.
        The log row already contains everything needed; no new Razorpay call is made.
    """
    log = db.query(RecoveryActionLog).filter(RecoveryActionLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Action log not found")

    # Guard 1: must be pending
    if log.outcome != "pending_approval":
        raise HTTPException(
            status_code=400,
            detail=f"Action is not pending approval (current outcome: {log.outcome!r}). "
                   "Double-approval is not allowed."
        )

    # Guard 2: merchant must own the case
    case = log.case
    if case is None or case.merchant_id != current_merchant.id:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to approve this action."
        )

    # Flip the flag first
    log.outcome = "approved"
    log.approved_by = current_merchant.id
    log.approved_at = datetime.now(timezone.utc)

    if case.status in (CaseStatus.NEW, CaseStatus.AT_RISK):
        case.status = CaseStatus.INTERVENING
        case.last_action_at = datetime.now(timezone.utc)

    db.commit()  # commit before sending notifications (idempotency boundary)

    # Fire the real notification. We retrieve the session from the case so we
    # can build the email / in-app message with the correct customer details.
    # If coupon or discount is present, sends full discount/coupon context.
    # Otherwise dispatches standard payment link notification.
    try:
        session = case.checkout_session
        if session and log.amount_offered is not None:
            customer_name = (
                session.customer_name
                or (case.customer.name if case.customer else None)
                or "Customer"
            )
            customer_email = (
                session.customer_email
                or (case.customer.email if case.customer else None)
            )
            store_name = current_merchant.store_name or "Our Store"
            cart_value = session.cart_value or case.amount_at_risk or 0.0
            amount_offered = log.amount_offered if log.amount_offered is not None else cart_value
            discount_amount = max(0.0, cart_value - amount_offered)

            guardrail_data = {}
            try:
                guardrail_data = json.loads(log.guardrail_checks or "{}")
            except Exception:
                pass

            base_url = get_base_url(request)
            link_url = (
                guardrail_data.get("payment_link_url")
                or guardrail_data.get("resume_url")
                or f"{base_url}/cart?resume={session.event_id}"
            )
            if log.coupon_code and "coupon=" not in link_url:
                separator = "&" if "?" in link_url else "?"
                link_url += f"{separator}coupon={log.coupon_code}"

            if log.coupon_code or discount_amount > 0:
                cart_items = []
                if session.cart_json:
                    try:
                        from app.db_models import Product
                        raw = json.loads(session.cart_json)
                        for ri in raw:
                            p = db.query(Product).filter(Product.id == ri.get("product_id")).first()
                            if p:
                                cart_items.append({
                                    "name": p.name,
                                    "price": p.price,
                                    "quantity": ri.get("quantity", 1),
                                })
                    except Exception:
                        pass

                context = {
                    "store_name": store_name,
                    "customer_name": customer_name,
                    "cart_items": cart_items,
                    "subtotal": cart_value,
                    "discount_amount": discount_amount,
                    "coupon_code": log.coupon_code,
                    "total": amount_offered,
                    "resume_url": link_url,
                    "message": f"An exclusive discount of coupon code {log.coupon_code} has been approved for your order!" if log.coupon_code else "An exclusive discount offer has been approved for your order.",
                }
                if customer_email:
                    send_recovery_email(
                        customer_email,
                        "Special Recovery Offer Approved — Complete Your Order",
                        "emails/recovery_email.html",
                        context,
                    )
                if session.customer_user_id or (case.customer and case.customer.id):
                    cid = session.customer_user_id or (case.customer.id if case.customer else None)
                    try:
                        from app.notification_service import send_in_app_notification
                        send_in_app_notification(
                            db,
                            cid,
                            "Special Offer Approved",
                            f"Your discount offer of code {log.coupon_code} is ready. Tap to complete your order." if log.coupon_code else "Your recovery offer is ready. Tap to complete your order.",
                            link_url,
                        )
                    except Exception:
                        pass
            else:
                from app.agent.payment_failure_actions import _send_payment_link_notification
                _send_payment_link_notification(session, case, link_url, current_merchant, db)
    except Exception as exc:
        # Non-fatal: the approval is already committed; notification failure
        # is logged but does not roll back the approval.
        import logging as _log
        _log.getLogger("recovery_agent.approve").warning(
            "Notification dispatch after approval of log %d failed: %s", log_id, exc
        )

    return {
        "status": "success",
        "message": "Action approved and notification dispatched",
        "outcome": log.outcome,
        "approved_by": log.approved_by,
        "approved_at": log.approved_at.isoformat() if log.approved_at else None,
    }


@router.post("/recovery-actions/{log_id}/reject")
def reject_recovery_action(
    log_id: int,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    """Merchant rejects a pending_approval recovery action.

    Guards:
      1. Log must exist.
      2. Log must be in outcome=pending_approval.
      3. Case must belong to this merchant.

    No notification is sent on rejection. The case remains in its current status.
    """
    log = db.query(RecoveryActionLog).filter(RecoveryActionLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Action log not found")

    if log.outcome != "pending_approval":
        raise HTTPException(
            status_code=400,
            detail=f"Action is not pending approval (current outcome: {log.outcome!r})."
        )

    # Merchant ownership check
    case = log.case
    if case is None or case.merchant_id != current_merchant.id:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to reject this action."
        )

    log.outcome = "rejected"
    log.approved_by = current_merchant.id   # "approved_by" records who acted; same field for reject
    log.approved_at = datetime.now(timezone.utc)
    db.commit()
    return {
        "status": "success",
        "message": "Action rejected",
        "outcome": log.outcome,
        "rejected_by": log.approved_by,
    }


@router.get("/coupon-recovery-usage")
def get_coupon_recovery_usage(
    scenario: Optional[str] = None,
    status: Optional[str] = None,
    coupon_code: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    """
    Returns recovery cases for current_merchant where a coupon was used,
    with summary KPI totals and filtering.
    Strict merchant isolation: RecoveryCase.merchant_id == current_merchant.id.
    """
    query = db.query(RecoveryCase).filter(
        RecoveryCase.merchant_id == current_merchant.id,
        RecoveryCase.coupon_code_used != None,
    )

    if status and status != "all":
        query = query.filter(RecoveryCase.status == status)
    elif status is None:
        # Default to RECOVERED if not specified
        query = query.filter(RecoveryCase.status == CaseStatus.RECOVERED)

    if scenario and scenario != "all":
        query = query.filter(RecoveryCase.scenario == scenario)

    if coupon_code and coupon_code.strip():
        query = query.filter(RecoveryCase.coupon_code_used.ilike(f"%{coupon_code.strip()}%"))

    if start_date:
        try:
            start_clean = start_date.strip().replace(" ", "+").replace("Z", "+00:00")
            sd = datetime.fromisoformat(start_clean)
            if sd.tzinfo is not None:
                sd = sd.astimezone(timezone.utc).replace(tzinfo=None)
            query = query.filter((RecoveryCase.last_action_at >= sd) | ((RecoveryCase.last_action_at == None) & (RecoveryCase.created_at >= sd)))
        except Exception:
            pass

    if end_date:
        try:
            end_clean = end_date.strip().replace(" ", "+").replace("Z", "+00:00")
            ed = datetime.fromisoformat(end_clean)
            if ed.tzinfo is not None:
                ed = ed.astimezone(timezone.utc).replace(tzinfo=None)
            query = query.filter((RecoveryCase.last_action_at <= ed) | ((RecoveryCase.last_action_at == None) & (RecoveryCase.created_at <= ed)))
        except Exception:
            pass

    cases = query.order_by(RecoveryCase.last_action_at.desc(), RecoveryCase.created_at.desc()).all()

    total_discount_amount = round(sum(c.discount_amount or 0.0 for c in cases), 2)
    total_recovered_amount = round(sum(c.amount_recovered or 0.0 for c in cases), 2)
    total_cases_count = len(cases)

    items = []
    for c in cases:
        customer_name = "Guest Customer"
        customer_email = "N/A"
        if c.customer:
            customer_name = c.customer.name or customer_name
            customer_email = c.customer.email or customer_email
        elif c.checkout_session:
            customer_name = c.checkout_session.customer_name or customer_name
            customer_email = c.checkout_session.customer_email or customer_email

        date_val = c.last_action_at or c.updated_at or c.created_at

        items.append({
            "id": c.id,
            "case_id": c.id,
            "date": date_val.isoformat() if date_val else None,
            "customer_name": customer_name,
            "customer_email": customer_email,
            "scenario": c.scenario.value if hasattr(c.scenario, "value") else str(c.scenario),
            "coupon_code": c.coupon_code_used,
            "discount_amount": round(c.discount_amount or 0.0, 2),
            "order_total": round(c.amount_recovered or 0.0, 2),
            "checkout_session_id": c.checkout_session_id,
        })

    return {
        "summary": {
            "total_discount_amount": total_discount_amount,
            "total_recovered_amount": total_recovered_amount,
            "total_coupon_recovered_cases": total_cases_count,
        },
        "cases": items,
    }


class IssueInvoiceRequest(BaseModel):
    customer_id: int
    amount: float
    due_date: Optional[str] = None
    due_in_minutes: Optional[int] = None
    invoice_number: Optional[str] = None
    currency: Optional[str] = "INR"


def _parse_due_date(due_date_str: Optional[str], due_in_minutes: Optional[int]) -> datetime:
    """Helper to compute actual datetime for invoice due_date.
    
    Supports:
    - Explicit due_in_minutes (integer)
    - Demo shortcuts: '5m', '15m', '30m', '1h', '1d', '7d', 'due in 5 minutes', etc.
    - ISO 8601 strings or YYYY-MM-DD strings
    - Default fallback: 7 days from now
    """
    now = datetime.now(timezone.utc)
    
    if due_in_minutes is not None and due_in_minutes > 0:
        return now + timedelta(minutes=due_in_minutes)
        
    if not due_date_str or not due_date_str.strip():
        return now + timedelta(days=7)
        
    s = due_date_str.strip().lower()
    
    # Shortcut mappings
    shortcuts = {
        "5m": 5, "5min": 5, "5_minutes": 5, "5 minutes": 5, "due in 5 minutes": 5,
        "10m": 10, "10min": 10, "10_minutes": 10, "10 minutes": 10, "due in 10 minutes": 10,
        "15m": 15, "15min": 15, "15_minutes": 15, "15 minutes": 15, "due in 15 minutes": 15,
        "30m": 30, "30min": 30, "30_minutes": 30, "30 minutes": 30, "due in 30 minutes": 30,
        "1h": 60, "1hour": 60, "1 hour": 60, "due in 1 hour": 60,
        "2h": 120, "2hours": 120, "2 hours": 120,
        "1d": 1440, "1day": 1440, "1 day": 1440, "due in 1 day": 1440,
        "7d": 10080, "7days": 10080, "7 days": 10080, "due in 7 days": 10080,
    }
    if s in shortcuts:
        return now + timedelta(minutes=shortcuts[s])
        
    if s.endswith("m") and s[:-1].isdigit():
        return now + timedelta(minutes=int(s[:-1]))
    if s.endswith("h") and s[:-1].isdigit():
        return now + timedelta(hours=int(s[:-1]))
    if s.endswith("d") and s[:-1].isdigit():
        return now + timedelta(days=int(s[:-1]))
        
    # ISO / Standard format parsing
    try:
        parsed = datetime.fromisoformat(due_date_str.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        pass
        
    try:
        parsed = datetime.strptime(due_date_str, "%Y-%m-%d")
        return parsed.replace(tzinfo=timezone.utc)
    except Exception:
        pass
        
    raise HTTPException(status_code=400, detail=f"Invalid due_date format: '{due_date_str}'. Use ISO format (YYYY-MM-DD), minutes, or a shortcut like '5m', '1h', '7d'.")


@router.post("/invoices")
def issue_invoice(
    payload: IssueInvoiceRequest,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant),
    razorpay_client=Depends(get_razorpay_client),
):
    """Issues a new merchant invoice, creates a real Razorpay Payment Link, and creates a corresponding RecoveryCase.
    
    Strictly scoped to current_merchant.id.
    """
    if payload.amount <= 0:
        raise HTTPException(status_code=400, detail="Invoice amount must be greater than 0")
        
    # Verify customer exists
    customer = db.query(CustomerUser).filter(CustomerUser.id == payload.customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail=f"Customer ID {payload.customer_id} not found")
        
    # Compute due date
    computed_due_date = _parse_due_date(payload.due_date, payload.due_in_minutes)
    
    now = datetime.now(timezone.utc)
    
    # Invoice number generation/validation
    if payload.invoice_number and payload.invoice_number.strip():
        inv_num = payload.invoice_number.strip()
        existing = db.query(Invoice).filter(Invoice.invoice_number == inv_num).first()
        if existing:
            raise HTTPException(status_code=400, detail=f"Invoice number '{inv_num}' already exists")
    else:
        inv_num = f"INV-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"

    # 1. Create real Razorpay Payment Link
    payment_link_url = None
    razorpay_inv_id = None
    try:
        base_url = get_base_url()
        ref_id = f"inv_{inv_num}_{uuid.uuid4().hex[:6]}"
        link_result = razorpay_client.create_recovery_payment_link(
            amount_rupees=float(payload.amount),
            customer_name=customer.name or "Customer",
            customer_email=customer.email or "",
            customer_phone=customer.phone or "+919999999999",
            description=f"Invoice {inv_num} Payment",
            reference_id=ref_id,
            notes={
                "invoice_number": inv_num,
                "merchant_id": str(current_merchant.id),
                "customer_id": str(customer.id),
            },
            callback_url=f"{base_url}/invoice/{inv_num}/confirmation",
            callback_method="get",
        )
        if link_result.success:
            payment_link_url = link_result.short_url or f"https://rzp.io/i/{link_result.payment_link_id}"
            razorpay_inv_id = link_result.payment_link_id
        else:
            logger.warning("Failed to create Razorpay payment link for invoice %s: %s", inv_num, link_result.error_message)
    except Exception as exc:
        logger.warning("Exception creating Razorpay payment link for invoice %s: %s", inv_num, exc)
        
    # 2. Create Invoice row
    invoice = Invoice(
        merchant_id=current_merchant.id,
        customer_user_id=customer.id,
        invoice_number=inv_num,
        amount=float(payload.amount),
        currency=payload.currency or "INR",
        issue_date=now,
        due_date=computed_due_date,
        status=InvoiceStatus.PENDING,
        payment_link_url=payment_link_url,
        razorpay_invoice_id=razorpay_inv_id,
    )
    db.add(invoice)
    db.flush()  # Populates invoice.id
    
    # 3. Immediately create matching RecoveryCase
    recovery_case = RecoveryCase(
        merchant_id=current_merchant.id,
        customer_user_id=customer.id,
        invoice_id=invoice.id,
        scenario=RecoveryScenario.OVERDUE_RECEIVABLE,
        amount_at_risk=float(payload.amount),
        amount_recovered=0.0,
        status=CaseStatus.NEW,
        ladder_step=0,
        created_at=now,
        updated_at=now,
    )
    db.add(recovery_case)
    db.commit()
    db.refresh(invoice)
    db.refresh(recovery_case)
    
    return {
        "status": "success",
        "invoice": {
            "id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "amount": invoice.amount,
            "currency": invoice.currency,
            "status": invoice.status.value if hasattr(invoice.status, "value") else str(invoice.status),
            "payment_link_url": invoice.payment_link_url,
            "razorpay_invoice_id": invoice.razorpay_invoice_id,
            "due_date": invoice.due_date.isoformat(),
            "issue_date": invoice.issue_date.isoformat(),
            "customer_id": invoice.customer_user_id,
            "customer_name": customer.name or "Guest",
            "customer_email": customer.email,
        },
        "recovery_case": {
            "id": recovery_case.id,
            "scenario": recovery_case.scenario.value if hasattr(recovery_case.scenario, "value") else str(recovery_case.scenario),
            "status": recovery_case.status.value if hasattr(recovery_case.status, "value") else str(recovery_case.status),
            "amount_at_risk": recovery_case.amount_at_risk,
            "invoice_id": recovery_case.invoice_id,
            "payment_link_url": invoice.payment_link_url,
        }
    }


@router.get("/invoices")
def get_invoices(
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    """List invoices belonging to the logged-in merchant."""
    invoices = db.query(Invoice).filter(Invoice.merchant_id == current_merchant.id).order_by(Invoice.created_at.desc()).all()
    results = []
    for inv in invoices:
        cust_name = "Guest"
        cust_email = None
        if inv.customer:
            cust_name = inv.customer.name or "Guest"
            cust_email = inv.customer.email
            
        case = db.query(RecoveryCase).filter(RecoveryCase.invoice_id == inv.id).first()
        
        results.append({
            "id": inv.id,
            "invoice_number": inv.invoice_number,
            "amount": inv.amount,
            "currency": inv.currency,
            "status": inv.status.value if hasattr(inv.status, "value") else str(inv.status),
            "issue_date": inv.issue_date.isoformat() if inv.issue_date else None,
            "due_date": inv.due_date.isoformat() if inv.due_date else None,
            "customer_id": inv.customer_user_id,
            "customer_name": cust_name,
            "customer_email": cust_email,
            "case_id": case.id if case else None,
            "case_status": case.status.value if (case and hasattr(case.status, "value")) else (str(case.status) if case else None),
        })
    return results





