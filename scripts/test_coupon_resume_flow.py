"""
Test Suite: Manual Recovery Offer & Coupon on Resume Link Flow (Option B)
========================================================================
Verifies:
  1. Merchant sends recovery offer with coupon via POST /api/merchant/manual-recovery
  2. Generated recovery email template contains updated copy:
     " — apply this code at checkout to redeem your discount"
  3. Customer validates coupon via POST /api/coupons/validate
  4. Customer starts checkout via POST /api/checkout/start with coupon_code and resume_event_id
  5. Checkout session applies coupon and calculates discounted amount
  6. Customer completes checkout via POST /api/checkout/complete
  7. RecoveryCase status transitions to RECOVERED with coupon_code_used and discount_amount populated
  8. GET /api/merchant/coupon-recovery-usage correctly reports coupon usage & discount totals
"""

import os
import sys
import json
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from app.db import get_db
from app.db_models import (
    Base, MerchantUser, CustomerUser, Product, CheckoutSession, SessionStatus,
    RecoveryCase, RecoveryActionLog, RecoveryScenario, CaseStatus,
    ClassificationMethod, Coupon, CouponUsageLog, RecoveryOutcomeRecord,
)
from app.razorpay_client import SimulatedRazorpayClient
from app.merchant_auth_routes import get_current_merchant
from app.customer_auth_routes import get_current_customer
from app.auth import create_merchant_session_token, MERCHANT_COOKIE_NAME
from app.main import app, get_razorpay_client
from app.email_service import templates

# ── In-memory DB ──────────────────────────────────────────────────────────────
engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
Base.metadata.create_all(bind=engine)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

# ── Seed data ─────────────────────────────────────────────────────────────────
db = TestingSessionLocal()

m1 = MerchantUser(id=1, email="merchant@example.com", password_hash="hash", store_name="Gadget Store", max_discount_pct=25)
db.add(m1)

cust = CustomerUser(id=1, email="cust@example.com", name="Bob Smith", phone="+919876543210", password_hash="hash")
db.add(cust)

prod1 = Product(id=1, merchant_id=1, name="Wireless Headphones", price=2000.0, stock=10, is_active=True)
db.add(prod1)

coupon = Coupon(id=1, merchant_id=1, code="SAVE15", discount_pct=15, active=True, usage_limit=100, times_used=0)
db.add(coupon)

# Original abandoned session
sess_orig = CheckoutSession(
    id=101, event_id="evt_orig_101", customer_user_id=1,
    customer_name="Bob Smith", customer_email="cust@example.com", customer_phone="+919876543210",
    cart_value=2000.0, final_amount_charged=2000.0,
    cart_json=json.dumps([{"product_id": 1, "name": "Wireless Headphones", "price": 2000.0, "quantity": 1}]),
    status=SessionStatus.ABANDONED, abandoned_at=datetime.now(timezone.utc),
)
db.add(sess_orig)

outcome = RecoveryOutcomeRecord(
    id=1, session_id=101, predicted_reason="price_shock_at_checkout", confidence=0.95,
    classification_method="rule", reasoning="Price shock cart abandonment",
    action_taken="email", action_success=True
)
db.add(outcome)

case = RecoveryCase(
    id=201, merchant_id=1, customer_user_id=1, checkout_session_id=101,
    scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
    amount_at_risk=2000.0, amount_recovered=0.0,
    status=CaseStatus.ESCALATED, ladder_step=2,
    classification="price_shock_at_checkout", classification_source=ClassificationMethod.RULE,
    escalated_to_human=True, contact_touches=2, last_action_at=datetime.now(timezone.utc),
)
db.add(case)
db.commit()
db.close()

# ── Auth & Dependency overrides ───────────────────────────────────────────────
def override_get_current_merchant(db: Session = Depends(override_get_db)):
    return db.query(MerchantUser).filter(MerchantUser.id == 1).first()

def override_get_current_customer(db: Session = Depends(override_get_db)):
    return db.query(CustomerUser).filter(CustomerUser.id == 1).first()

def override_get_razorpay_client():
    return SimulatedRazorpayClient()

app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[get_current_merchant] = override_get_current_merchant
app.dependency_overrides[get_current_customer] = override_get_current_customer
app.dependency_overrides[get_razorpay_client] = override_get_razorpay_client

token = create_merchant_session_token(1)
client = TestClient(app, cookies={MERCHANT_COOKIE_NAME: token})

# ── Helpers ───────────────────────────────────────────────────────────────────
PASS = "✅ PASS"
FAIL = "❌ FAIL"
failures = []

def check(name, condition, detail=""):
    if condition:
        print(f"  {PASS}  {name}")
    else:
        print(f"  {FAIL}  {name}  ({detail!r})")
        failures.append(f"{name}: {detail!r}")

print("=" * 70)
print("Manual Recovery Offer & Coupon Resume Link Flow Tests")
print("=" * 70)

# ── [1] Merchant sends manual recovery offer with coupon SAVE15 ───────────────
print("\n[1] Merchant sends manual recovery offer with coupon SAVE15")
with patch("app.merchant_extensions.send_recovery_email", return_value=True):
    res_offer = client.post(
        "/api/merchant/manual-recovery",
        json={
            "case_id": 201,
            "session_id": 101,
            "channel": "email",
            "custom_message": "Special offer for your cart!",
            "coupon_id": 1,
        }
    )
check("manual recovery returns 200", res_offer.status_code == 200, res_offer.status_code)
data_offer = res_offer.json()
check("status is success", data_offer.get("status") == "success")

# Verify action log
db = TestingSessionLocal()
log = db.query(RecoveryActionLog).filter(
    RecoveryActionLog.case_id == 201,
    RecoveryActionLog.action_type == "manual_email"
).first()
check("action log created for manual offer", log is not None)
if log:
    guard = json.loads(log.guardrail_checks or "{}")
    check("discount_allowed is True", guard.get("discount_allowed") is True, guard)
    check("amount_offered == 1700.0", guard.get("amount_offered") == 1700.0, guard)
db.close()

# ── [2] Verify recovery email template copy ───────────────────────────────────
print("\n[2] Verify email template copy")
context = {
    "store_name": "Gadget Store",
    "customer_name": "Bob Smith",
    "message": "Special offer for your cart!",
    "cart_items": [{"name": "Wireless Headphones", "price": 2000.0, "quantity": 1}],
    "subtotal": 2000.0,
    "total": 1700.0,
    "resume_url": "http://localhost:8001/cart?resume=evt_orig_101&coupon=SAVE15",
    "coupon_code": "SAVE15",
    "discount_amount": 300.0
}
email_html = templates.get_template("emails/recovery_email.html").render(context)
check("email copy says 'apply this code at checkout'", " — apply this code at checkout to redeem your discount" in email_html)
check("old copy 'already applied to your link below' is NOT present", "already applied to your link below" not in email_html)

# ── [3] Customer validates coupon ─────────────────────────────────────────────
print("\n[3] Customer validates coupon SAVE15 via POST /api/coupons/validate")
res_val = client.post(
    "/api/coupons/validate",
    json={"code": "SAVE15", "cart_total": 2000.0}
)
check("coupon validate returns 200", res_val.status_code == 200, res_val.status_code)
val_data = res_val.json()
check("discount amount is 300.0 (15% of 2000)", val_data.get("discount_amount") == 300.0, val_data)
check("new total is 1700.0", val_data.get("new_total") == 1700.0, val_data)

# ── [4] Customer starts checkout with coupon_code and resume_event_id ─────────
print("\n[4] Customer starts checkout with coupon_code and resume_event_id")
res_chk = client.post(
    "/api/checkout/start",
    json={
        "cart_items": [{"product_id": 1, "quantity": 1}],
        "resume_event_id": "evt_orig_101",
        "coupon_code": "SAVE15"
    }
)
check("checkout start returns 200", res_chk.status_code == 200, res_chk.status_code)
chk_data = res_chk.json()
check("amount_paise is 170000 (1700 INR)", chk_data.get("amount_paise") == 170000, chk_data.get("amount_paise"))
resumed_event_id = chk_data.get("event_id")
order_id = chk_data.get("order_id")

# Verify CheckoutSession saved with applied_coupon_id
db = TestingSessionLocal()
new_sess = db.query(CheckoutSession).filter(CheckoutSession.event_id == resumed_event_id).first()
check("CheckoutSession created with recovered_from_session_id=101", new_sess and new_sess.recovered_from_session_id == 101)
check("CheckoutSession has applied_coupon_id=1", new_sess and new_sess.applied_coupon_id == 1)
check("CheckoutSession final_amount_charged=1700.0", new_sess and new_sess.final_amount_charged == 1700.0)
db.close()

# ── [5] Customer completes checkout ───────────────────────────────────────────
print("\n[5] Customer completes checkout via POST /api/checkout/complete")
res_comp = client.post(
    "/api/checkout/complete",
    json={
        "event_id": resumed_event_id,
        "razorpay_order_id": order_id,
        "razorpay_payment_id": "pay_test_resume_123",
        "razorpay_signature": "mock_sig_resume",
    }
)
check("checkout complete returns 200", res_comp.status_code == 200, res_comp.status_code)

# ── [6] Verify RecoveryCase updated to RECOVERED with coupon data ──────────────
print("\n[6] Verify RecoveryCase updated to RECOVERED with coupon data")
db = TestingSessionLocal()
rec_case = db.query(RecoveryCase).filter(RecoveryCase.id == 201).first()
check("RecoveryCase status is RECOVERED", rec_case.status == CaseStatus.RECOVERED, rec_case.status)
check("RecoveryCase amount_recovered is 1700.0", rec_case.amount_recovered == 1700.0, rec_case.amount_recovered)
check("RecoveryCase coupon_code_used is 'SAVE15'", rec_case.coupon_code_used == "SAVE15", rec_case.coupon_code_used)
check("RecoveryCase discount_amount is 300.0", rec_case.discount_amount == 300.0, rec_case.discount_amount)
db.close()

# ── [7] Verify Merchant Coupon Usage endpoint ─────────────────────────────────
print("\n[7] Verify Merchant Coupon Usage endpoint")
res_usage = client.get("/api/merchant/coupon-recovery-usage")
check("coupon usage endpoint returns 200", res_usage.status_code == 200)
usage_data = res_usage.json()
check("total_discount_amount == 300.0", usage_data["summary"]["total_discount_amount"] == 300.0, usage_data["summary"])
check("total_recovered_amount == 1700.0", usage_data["summary"]["total_recovered_amount"] == 1700.0, usage_data["summary"])
check("total_coupon_recovered_cases == 1", usage_data["summary"]["total_coupon_recovered_cases"] == 1)
check("case coupon code is SAVE15", usage_data["cases"][0]["coupon_code"] == "SAVE15")

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'=' * 70}")
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  ❌ {f}")
    sys.exit(1)
else:
    print("RESULT: ALL COUPON RESUME FLOW TESTS PASSED ✅")
print("=" * 70)
