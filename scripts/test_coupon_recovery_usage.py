"""
Test Suite: Coupon Recovery Usage Reporting & RECOVERED Transition with Coupons
================================================================================
Verifies:
  1. complete_checkout populates coupon_code_used and discount_amount on RecoveryCase
     when a payment succeeds with an applied coupon:
     - For both abandonment-recovery path (via recovered_from_session_id)
     - And payment-failure recovery path (via same session)
  2. GET /api/merchant/coupon-recovery-usage returns correct summary KPIs:
     - total_discount_amount
     - total_recovered_amount
     - total_coupon_recovered_cases
  3. GET /api/merchant/coupon-recovery-usage filters work:
     - scenario filter
     - coupon_code filter
  4. Strict merchant data isolation: Merchant 1 cannot see Merchant 2's coupon recoveries.
  5. HTML page GET /merchant/coupons returns 200 OK.
"""

import os
import sys
import json
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from app.db import get_db
from app.db_models import (
    Base, MerchantUser, CustomerUser, CheckoutSession, SessionStatus,
    RecoveryCase, RecoveryActionLog, RecoveryScenario, CaseStatus,
    ClassificationMethod, Coupon, CouponUsageLog,
)
from app.razorpay_client import SimulatedRazorpayClient
from app.merchant_auth_routes import get_current_merchant
from app.customer_auth_routes import get_current_customer
from app.auth import create_merchant_session_token, MERCHANT_COOKIE_NAME
from app.main import app, get_razorpay_client

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

m1 = MerchantUser(id=1, email="m1@example.com", password_hash="hash", store_name="Store One", max_discount_pct=20)
m2 = MerchantUser(id=2, email="m2@example.com", password_hash="hash", store_name="Store Two", max_discount_pct=20)
db.add_all([m1, m2])

cust = CustomerUser(id=1, email="cust@example.com", name="Alice Wonderland", phone="+919999999999", password_hash="hash")
db.add(cust)

# Coupons
cp1 = Coupon(id=1, merchant_id=1, code="SAVE10", discount_pct=10, active=True)
cp2 = Coupon(id=2, merchant_id=1, code="FLAT500", discount_amount=500.0, active=True)
cp_m2 = Coupon(id=3, merchant_id=2, code="M2_SECRET", discount_pct=15, active=True)
db.add_all([cp1, cp2, cp_m2])

# ── Scenario A: Abandonment Recovery Case (Case 301, Session 501 abandoned, Session 502 paid with SAVE10)
sess_orig_ab = CheckoutSession(
    id=501, event_id="evt_orig_ab_501", customer_user_id=1,
    customer_name="Alice Wonderland", customer_email="cust@example.com", customer_phone="+919999999999",
    cart_value=4000.0, final_amount_charged=4000.0, cart_json=json.dumps([]), status=SessionStatus.ABANDONED,
)
case_ab = RecoveryCase(
    id=301, merchant_id=1, customer_user_id=1, checkout_session_id=501,
    scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
    amount_at_risk=4000.0, amount_recovered=0.0,
    status=CaseStatus.INTERVENING, ladder_step=1,
    classification="price_sensitive", classification_source=ClassificationMethod.RULE,
    contact_touches=1, last_action_at=datetime.now(timezone.utc),
)
# New checkout session that recovered the cart with 10% coupon (charged 3600, discount 400)
sess_new_ab = CheckoutSession(
    id=502, event_id="evt_new_ab_502", customer_user_id=1,
    customer_name="Alice Wonderland", customer_email="cust@example.com", customer_phone="+919999999999",
    cart_value=4000.0, final_amount_charged=3600.0, cart_json=json.dumps([]), status=SessionStatus.STARTED,
    recovered_from_session_id=501, applied_coupon_id=1,
)

# ── Scenario B: Payment Failure Recovery Case (Case 302, Session 503 retried with FLAT500)
sess_pf = CheckoutSession(
    id=503, event_id="evt_pf_503", customer_user_id=1,
    customer_name="Alice Wonderland", customer_email="cust@example.com", customer_phone="+919999999999",
    cart_value=2500.0, final_amount_charged=2000.0, cart_json=json.dumps([]), status=SessionStatus.STARTED,
    applied_coupon_id=2,
)
case_pf = RecoveryCase(
    id=302, merchant_id=1, customer_user_id=1, checkout_session_id=503,
    scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=2500.0, amount_recovered=0.0,
    status=CaseStatus.INTERVENING, ladder_step=1,
    classification="bank_gateway_failure", classification_source=ClassificationMethod.RULE,
    contact_touches=1, last_action_at=datetime.now(timezone.utc),
)

# ── Scenario C: Merchant 2 Recovery Case (Case 303, Session 504 with M2_SECRET)
sess_m2 = CheckoutSession(
    id=504, event_id="evt_m2_504", customer_user_id=1,
    customer_name="Alice Wonderland", customer_email="cust@example.com", customer_phone="+919999999999",
    cart_value=6000.0, final_amount_charged=5100.0, cart_json=json.dumps([]), status=SessionStatus.STARTED,
    applied_coupon_id=3,
)
case_m2 = RecoveryCase(
    id=303, merchant_id=2, customer_user_id=1, checkout_session_id=504,
    scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
    amount_at_risk=6000.0, amount_recovered=5100.0,
    coupon_code_used="M2_SECRET", discount_amount=900.0,
    status=CaseStatus.RECOVERED, ladder_step=1,
    classification="price_sensitive", classification_source=ClassificationMethod.RULE,
    contact_touches=1, last_action_at=datetime.now(timezone.utc),
)

db.add_all([sess_orig_ab, case_ab, sess_new_ab, sess_pf, case_pf, sess_m2, case_m2])
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
print("Coupon Recovery Usage & Complete Checkout Tests")
print("=" * 70)

# ── [1] complete_checkout for Abandonment Recovery (Session 502 with SAVE10) ──
print("\n[1] complete_checkout for Abandonment Recovery with coupon SAVE10")
res = client.post(
    "/api/checkout/complete",
    json={
        "event_id": "evt_new_ab_502",
        "razorpay_order_id": "order_502",
        "razorpay_payment_id": "pay_502",
        "razorpay_signature": "mock_sig_502",
    }
)
check("complete_checkout returns 200", res.status_code == 200, res.status_code)

db = TestingSessionLocal()
c301 = db.query(RecoveryCase).filter(RecoveryCase.id == 301).first()
check("c301.status == CaseStatus.RECOVERED", c301.status == CaseStatus.RECOVERED, c301.status)
check("c301.amount_recovered == 3600.0", c301.amount_recovered == 3600.0, c301.amount_recovered)
check("c301.coupon_code_used == 'SAVE10'", c301.coupon_code_used == "SAVE10", c301.coupon_code_used)
check("c301.discount_amount == 400.0", c301.discount_amount == 400.0, c301.discount_amount)

# Verify audit log
log301 = db.query(RecoveryActionLog).filter(
    RecoveryActionLog.case_id == 301,
    RecoveryActionLog.action_type == "case_recovered"
).first()
check("audit log written for case 301", log301 is not None)
if log301:
    check("audit log records coupon_code", log301.coupon_code == "SAVE10", log301.coupon_code)
    guard = json.loads(log301.guardrail_checks or "{}")
    check("audit guardrail discount_amount == 400.0", guard.get("discount_amount") == 400.0, guard)
db.close()

# ── [2] complete_checkout for Payment Failure (Session 503 with FLAT500) ───────
print("\n[2] complete_checkout for Payment Failure with coupon FLAT500")
res = client.post(
    "/api/checkout/complete",
    json={
        "event_id": "evt_pf_503",
        "razorpay_order_id": "order_503",
        "razorpay_payment_id": "pay_503",
        "razorpay_signature": "mock_sig_503",
    }
)
check("complete_checkout returns 200", res.status_code == 200, res.status_code)

db = TestingSessionLocal()
c302 = db.query(RecoveryCase).filter(RecoveryCase.id == 302).first()
check("c302.status == CaseStatus.RECOVERED", c302.status == CaseStatus.RECOVERED, c302.status)
check("c302.amount_recovered == 2000.0", c302.amount_recovered == 2000.0, c302.amount_recovered)
check("c302.coupon_code_used == 'FLAT500'", c302.coupon_code_used == "FLAT500", c302.coupon_code_used)
check("c302.discount_amount == 500.0", c302.discount_amount == 500.0, c302.discount_amount)
db.close()

# ── [3] GET /api/merchant/coupon-recovery-usage — Overall & KPIs ───────────────
print("\n[3] GET /api/merchant/coupon-recovery-usage")
res = client.get("/api/merchant/coupon-recovery-usage")
check("status 200", res.status_code == 200, res.status_code)
data = res.json()

summary = data.get("summary", {})
# Total discounts = 400 (case 301) + 500 (case 302) = 900.0
check("total_discount_amount == 900.0", summary.get("total_discount_amount") == 900.0, summary.get("total_discount_amount"))
# Total recovered = 3600 (case 301) + 2000 (case 302) = 5600.0
check("total_recovered_amount == 5600.0", summary.get("total_recovered_amount") == 5600.0, summary.get("total_recovered_amount"))
check("total_coupon_recovered_cases == 2", summary.get("total_coupon_recovered_cases") == 2, summary.get("total_coupon_recovered_cases"))

cases = data.get("cases", [])
check("cases length == 2", len(cases) == 2, len(cases))
codes = {c["coupon_code"] for c in cases}
check("both SAVE10 and FLAT500 present", codes == {"SAVE10", "FLAT500"}, codes)

# ── [4] Filtering by Scenario ─────────────────────────────────────────────────
print("\n[4] Filter by scenario")
res_pf = client.get("/api/merchant/coupon-recovery-usage?scenario=payment_failure")
check("status 200", res_pf.status_code == 200)
data_pf = res_pf.json()
check("filtered pf cases length == 1", len(data_pf.get("cases", [])) == 1, len(data_pf.get("cases", [])))
check("filtered pf coupon == 'FLAT500'", data_pf["cases"][0]["coupon_code"] == "FLAT500")

res_ab = client.get("/api/merchant/coupon-recovery-usage?scenario=checkout_abandonment")
check("filtered ab cases length == 1", len(res_ab.json().get("cases", [])) == 1)
check("filtered ab coupon == 'SAVE10'", res_ab.json()["cases"][0]["coupon_code"] == "SAVE10")

# ── [5] Filtering by Coupon Code ──────────────────────────────────────────────
print("\n[5] Filter by coupon code")
res_code = client.get("/api/merchant/coupon-recovery-usage?coupon_code=SAVE")
check("status 200", res_code.status_code == 200)
check("matched SAVE cases count == 1", len(res_code.json().get("cases", [])) == 1)
check("matched coupon code == 'SAVE10'", res_code.json()["cases"][0]["coupon_code"] == "SAVE10")

# ── [6] Strict Merchant Data Isolation ────────────────────────────────────────
print("\n[6] Strict merchant data isolation")
# Merchant 2 has Case 303 (with M2_SECRET and discount 900)
# Merchant 1 MUST NOT see M2_SECRET or case 303 in the list
all_codes_m1 = {c["coupon_code"] for c in cases}
check("M2_SECRET is ABSENT from merchant 1's view", "M2_SECRET" not in all_codes_m1, all_codes_m1)
case_ids_m1 = {c["case_id"] for c in cases}
check("case 303 is ABSENT from merchant 1's view", 303 not in case_ids_m1, case_ids_m1)

# Now log in as Merchant 2 and check their view
token_m2 = create_merchant_session_token(2)
client_m2 = TestClient(app, cookies={MERCHANT_COOKIE_NAME: token_m2})
def override_get_current_merchant_m2(db: Session = Depends(override_get_db)):
    return db.query(MerchantUser).filter(MerchantUser.id == 2).first()
app.dependency_overrides[get_current_merchant] = override_get_current_merchant_m2

res_m2 = client_m2.get("/api/merchant/coupon-recovery-usage")
check("merchant 2 status 200", res_m2.status_code == 200)
data_m2 = res_m2.json()
check("merchant 2 sees only their 1 case", len(data_m2.get("cases", [])) == 1, len(data_m2.get("cases", [])))
check("merchant 2 coupon == 'M2_SECRET'", data_m2["cases"][0]["coupon_code"] == "M2_SECRET")
check("merchant 2 total_discount_amount == 900.0", data_m2["summary"]["total_discount_amount"] == 900.0)

# ── [7] HTML Page GET /merchant/coupons (Redirects to Settings) ───────────────
print("\n[7] HTML Page GET /merchant/coupons")
res_page = client.get("/merchant/coupons", follow_redirects=False)
check("redirect status 302/307", res_page.status_code in (302, 307))
check("redirect location is /merchant/settings#coupon-usage", res_page.headers.get("location") == "/merchant/settings#coupon-usage")

res_followed = client.get("/merchant/coupons", follow_redirects=True)
check("followed page status 200", res_followed.status_code == 200)
check("followed page contains 'Coupon Usage History'", "Coupon Usage History" in res_followed.text)
check("followed page contains KPI cards", "kpi-total-discounts" in res_followed.text)

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'=' * 70}")
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  ❌ {f}")
    sys.exit(1)
else:
    print("RESULT: ALL COUPON RECOVERY USAGE TESTS PASSED ✅")
print("=" * 70)
