import os
import json
from sqlalchemy.orm import Session
from app.db_models import CheckoutSession, RecoveryOutcomeRecord, MerchantUser, CustomerUser
from app.email_service import send_recovery_email

REASON_RULES = {
    "bank_failure": {"allowed_actions": ["send_reminder_email"], "allow_discount": False},
    "gateway_error": {"allowed_actions": ["send_reminder_email"], "allow_discount": False},
    "network_drop": {"allowed_actions": ["send_reminder_email"], "allow_discount": False},
    "card_declined": {"allowed_actions": ["send_reminder_email_alternate_payment"], "allow_discount": False},
    "insufficient_funds": {"allowed_actions": ["send_reminder_email_alternate_payment"], "allow_discount": False},
    "otp_timeout": {"allowed_actions": ["send_reminder_email"], "allow_discount": False},
    "price_shock_at_checkout": {"allowed_actions": ["send_discount_offer_email", "send_reminder_email"], "allow_discount": True},
    "high_amount_hesitation": {"allowed_actions": ["send_discount_offer_email", "send_reminder_email"], "allow_discount": True},
    "accidental_close": {"allowed_actions": ["send_reminder_email"], "allow_discount": False},
    "unknown": {"allowed_actions": ["send_reminder_email"], "allow_discount": False, "flag_manual": True},
}

def decide_intervention(session: CheckoutSession, reason: str, merchant: MerchantUser, customer: CustomerUser) -> tuple[str, str, float]:
    """
    Returns (action, reasoning, discount_pct)
    """
    if customer and customer.opted_out_of_marketing:
        return 'suppress', "Customer opted out of marketing", 0.0
        
    if session.previous_recovery_attempts >= merchant.max_recovery_attempts:
        return 'suppress', "max_attempts_reached", 0.0
        
    rule = REASON_RULES.get(reason, REASON_RULES["unknown"])
    
    if rule["allow_discount"]:
        # Offer min_discount_pct as a safe default, or max_discount_pct if we really want to push it.
        # We will just offer min_discount_pct for now unless cart value is high, then maybe max?
        # Let's offer min_discount_pct to be safe.
        discount_pct = float(merchant.min_discount_pct)
        action = "send_discount_offer_email" if "send_discount_offer_email" in rule["allowed_actions"] else rule["allowed_actions"][0]
        return action, f"Allowed discount due to {reason}", discount_pct
    else:
        action = rule["allowed_actions"][0]
        return action, f"Denied discount: {reason}", 0.0

def execute_intervention(session: CheckoutSession, reason: str, action: str, reasoning: str, discount_pct: float, confidence: float, method: str) -> RecoveryOutcomeRecord:
    """
    Executes the intervention and returns a RecoveryOutcomeRecord.
    """
    outcome = RecoveryOutcomeRecord(
        session_id=session.id,
        predicted_reason=reason,
        confidence=confidence,
        classification_method=method,
        reasoning=reasoning,
        action_taken=action,
        action_success=True,
        delivery_status='suppressed'
    )
    
    if action == 'suppress':
        return outcome
        
    from app.config import get_base_url
    resume_link = f"{get_base_url()}/cart?resume={session.event_id}"
    outcome.resume_url = resume_link
    
    cart_items = []
    try:
        cart_items = json.loads(session.cart_json) if session.cart_json else []
    except Exception:
        pass
        
    cart_html = "<ul>"
    for item in cart_items:
        cart_html += f"<li>{item.get('name', 'Product')} x {item.get('quantity', 1)} - ₹{item.get('price', 0)}</li>"
    cart_html += "</ul>"
    
    subject = "Complete your purchase at ShopDemo"
    body = f"<p>Hi {session.customer_name or 'there'},</p>"
    body += "<p>You left some items in your cart:</p>"
    body += cart_html
    body += f"<p>Original Total: ₹{session.cart_value}</p>"
    
    if action == 'send_discount_offer_email' and discount_pct > 0:
        new_total = session.cart_value * (1 - discount_pct / 100)
        outcome.amount_offered = new_total
        body += f"<p>Good news! We're offering a {discount_pct}% discount. Your new total is ₹{new_total:.2f}.</p>"
        
    if "alternate_payment" in action:
        body += "<p>If you had trouble with your card, you can try using UPI or a different card!</p>"
        
    body += f'<p><a href="{resume_link}">Click here to resume your checkout</a></p>'
    
    success = send_recovery_email(session.customer_email, subject, body)
    
    outcome.action_success = success
    outcome.delivery_status = 'sent' if success else 'failed'
    
    if not success and 'RESEND_API_KEY' not in os.environ:
        outcome.delivery_status = 'skipped_no_key'
        
    return outcome
