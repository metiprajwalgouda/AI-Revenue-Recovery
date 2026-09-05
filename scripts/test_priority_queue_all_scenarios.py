"""
Test suite for Priority Queue with Multi-Scenario Escalations
=============================================================
Tests:
  1. GET /merchant/priority (HTML Page 200 OK)
  2. GET /api/merchant/priority returns escalated cases across ALL scenarios
  3. POST /api/merchant/priority/{id}/contact resolves escalation
  4. POST /api/merchant/priority/{id}/call initiates outreach
"""

import os
import sys
import json
from datetime import datetime, timezone

os.environ["MOCK_CALLS"] = "true"
os.environ["RECOVERY_CALL_PROVIDER"] = "exotel"
os.environ["EXOTEL_SID"] = "mock_sid"
os.environ["EXOTEL_API_KEY"] = "mock_key"
os.environ["EXOTEL_API_TOKEN"] = "mock_token"
os.environ["EXOTEL_PHONE_NUMBER"] = "+919999999999"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from app.db import get_db
from app.db_models import (
    Base, MerchantUser, CustomerUser, CheckoutSession, SessionStatus,
    RecoveryCase, RecoveryActionLog, RecoveryScenario, CaseStatus, ClassificationMethod
)
from app.merchant_auth_routes import get_current_merchant
from app.auth import create_merchant_session_token, MERCHANT_COOKIE_NAME
from app.main import app

# Set up in-memory DB
engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)
Base.metadata.create_all(bind=engine)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

# Seed test merchant & customers
db = TestingSessionLocal()
merchant = MerchantUser(
    id=1, email="merchant@example.com", password_hash="hash",
    store_name="Demo Store", max_discount_pct=15
)
db.add(merchant)

cust1 = CustomerUser(
    id=1, email="alice@example.com", name="Alice Wonderland",
    phone="+919876543210", password_hash="hash"
)
cust2 = CustomerUser(
    id=2, email="bob@example.com", name="Bob Builder",
    phone="+919123456780", password_hash="hash"
)
cust3 = CustomerUser(
    id=3, email="charlie@example.com", name="Charlie Brown",
    phone="+919999888877", password_hash="hash"
)
db.add_all([cust1, cust2, cust3])
db.commit()

# 1. Escalated Payment Failure case (risk terminal)
pf_case = RecoveryCase(
    id=301,
    merchant_id=1,
    customer_user_id=1,
    scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=8500.0,
    status=CaseStatus.ESCALATED,
    ladder_step=1,
    classification="risk_terminal",
    classification_source=ClassificationMethod.RULE,
    error_source="business",
    rar_score=100.0,
    confidence=1.0,
    escalated_to_human=True,
    escalation_reason="risk_block: velocity threshold exceeded",
    contact_touches=0,
)
db.add(pf_case)

# 2. Escalated Checkout Abandonment case
ab_case = RecoveryCase(
    id=302,
    merchant_id=1,
    customer_user_id=2,
    scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
    amount_at_risk=15000.0,
    status=CaseStatus.ESCALATED,
    ladder_step=1,
    classification="price_shock_at_checkout",
    classification_source=ClassificationMethod.LLM,
    error_source=None,
    rar_score=95.0,
    confidence=0.85,
    escalated_to_human=True,
    escalation_reason="high_value_customer_request",
    contact_touches=1,
)
db.add(ab_case)

# 3. Escalated Overdue Receivable case
rec_case = RecoveryCase(
    id=303,
    merchant_id=1,
    customer_user_id=3,
    invoice_id=901,
    scenario=RecoveryScenario.OVERDUE_RECEIVABLE,
    amount_at_risk=25000.0,
    status=CaseStatus.ESCALATED,
    ladder_step=2,
    classification="disputed_invoice",
    classification_source=ClassificationMethod.RULE,
    error_source="customer",
    rar_score=90.0,
    confidence=1.0,
    escalated_to_human=True,
    escalation_reason="invoice_dispute: awaiting manual review",
    contact_touches=2,
)
db.add(rec_case)

# 4. Standard high-priority CheckoutSession (VIP threshold)
sess_vip = CheckoutSession(
    id=401,
    event_id="evt_vip_401",
    customer_user_id=1,
    customer_name="Alice VIP",
    customer_email="alice@example.com",
    customer_phone="+919876543210",
    cart_value=12000.0,
    status=SessionStatus.ABANDONED,
    is_high_priority=True,
)
db.add(sess_vip)

db.commit()
db.close()

def override_get_current_merchant(db: Session = Depends(get_db)):
    return db.query(MerchantUser).filter(MerchantUser.id == 1).first()

app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[get_current_merchant] = override_get_current_merchant

token = create_merchant_session_token(1)
client = TestClient(app, cookies={MERCHANT_COOKIE_NAME: token})

PASS = "✅ PASS"
FAIL = "❌ FAIL"
failures = []

def check(name, condition, detail=""):
    if condition:
        print(f"  {PASS}  {name}")
    else:
        print(f"  {FAIL}  {name}  {detail}")
        failures.append(f"{name}: {detail}")

print("=" * 70)
print("Priority Follow-ups Multi-Scenario Escalation Test Suite")
print("=" * 70)

# [1] HTML Page
print("\n[1] GET /merchant/priority (HTML Page)")
res = client.get("/merchant/priority")
check("status_code == 200", res.status_code == 200, res.status_code)
check("contains 'Scenario' in table headers", "Scenario" in res.text)
check("contains scenario filter", "scenario-filter" in res.text)

# [2] Priority Queue API
print("\n[2] GET /api/merchant/priority (All Scenarios)")
res = client.get("/api/merchant/priority")
check("status_code == 200", res.status_code == 200, res.status_code)
queue = res.json()
check("total queue length == 4", len(queue) == 4, len(queue))

# Check scenarios present
scenarios_present = {item["scenario"] for item in queue}
check("payment_failure in queue", "payment_failure" in scenarios_present)
check("checkout_abandonment in queue", "checkout_abandonment" in scenarios_present)
check("overdue_receivable in queue", "overdue_receivable" in scenarios_present)

# Verify details of payment failure escalation
pf_item = next((i for i in queue if i.get("case_id") == 301), None)
check("pf_item customer == 'Alice Wonderland'", pf_item and pf_item["customer_name"] == "Alice Wonderland")
check("pf_item cart_value == 8500.0", pf_item and pf_item["cart_value"] == 8500.0)
check("pf_item is_escalated_case == True", pf_item and pf_item["is_escalated_case"] is True)

# Verify details of overdue receivable escalation
rec_item = next((i for i in queue if i.get("case_id") == 303), None)
check("rec_item scenario == 'overdue_receivable'", rec_item and rec_item["scenario"] == "overdue_receivable")
check("rec_item amount == 25000.0", rec_item and rec_item["cart_value"] == 25000.0)

# [3] Test Contact Endpoint on Standalone RecoveryCase
print("\n[3] POST /api/merchant/priority/301/contact (Resolve escalation on case)")
res = client.post("/api/merchant/priority/301/contact")
check("status_code == 200", res.status_code == 200, res.status_code)

# Verify in DB that case 301 escalated_to_human is now False
db = TestingSessionLocal()
updated_case = db.query(RecoveryCase).filter(RecoveryCase.id == 301).first()
check("case 301 escalated_to_human == False", updated_case.escalated_to_human is False)
check("case 301 status == 'intervening'", updated_case.status == CaseStatus.INTERVENING)
db.close()

# Verify queue now has 3 items
res = client.get("/api/merchant/priority")
check("queue length now 3 after resolving 301", len(res.json()) == 3, len(res.json()))

# [4] Test Call Endpoint on Standalone RecoveryCase
print("\n[4] POST /api/merchant/priority/case_303/call (Initiate call for case 303)")
res = client.post("/api/merchant/priority/case_303/call")
check("status_code == 200", res.status_code == 200, res.status_code)
check("call status returned (skipped_no_key or success)", res.json().get("status") in ("skipped_no_key", "success", "failed"))

# Verify touches incremented and ActionLog written for case 303
db = TestingSessionLocal()
updated_rec = db.query(RecoveryCase).filter(RecoveryCase.id == 303).first()
check("case 303 contact_touches incremented to 3", updated_rec.contact_touches == 3, updated_rec.contact_touches)
log_call = db.query(RecoveryActionLog).filter(RecoveryActionLog.case_id == 303, RecoveryActionLog.action_type == "manual_call").first()
check("manual_call logged to RecoveryActionLog", log_call is not None)
check("manual_call approved_by set", log_call and log_call.approved_by == 1)
db.close()

# [5] Test Manual Recovery ("Send Offer") writes RecoveryActionLog
print("\n[5] POST /api/merchant/manual-recovery (Send Offer writes RecoveryActionLog)")
from unittest.mock import patch

with patch("app.merchant_extensions.send_recovery_email", return_value=True):
    res = client.post("/api/merchant/manual-recovery", json={
        "case_id": 302,
        "channel": "email",
        "custom_message": "Special 10% discount to complete your checkout!",
        "discount_pct": 10
    })
check("send offer status_code == 200", res.status_code == 200, res.status_code)
check("send offer success == True", res.json().get("success") is True)

db = TestingSessionLocal()
case_302 = db.query(RecoveryCase).filter(RecoveryCase.id == 302).first()
check("case 302 escalated_to_human resolved to False", case_302.escalated_to_human is False)
check("case 302 status is INTERVENING", case_302.status == CaseStatus.INTERVENING)
offer_log = db.query(RecoveryActionLog).filter(RecoveryActionLog.case_id == 302, RecoveryActionLog.action_type == "manual_email").first()
check("manual_email logged in RecoveryActionLog", offer_log is not None)
check("offer_log requires_human_approval is False", offer_log and offer_log.requires_human_approval is False)
check("offer_log approved_by is current_merchant (1)", offer_log and offer_log.approved_by == 1)
check("offer_log reason contains custom message", offer_log and "Special 10% discount" in offer_log.reason)
db.close()

# [6] Test ID Collision Disambiguation (CheckoutSession 401 vs hypothetical RecoveryCase 401)
print("\n[6] ID Collision Disambiguation (case_401 vs session_401)")
db = TestingSessionLocal()
case_401 = RecoveryCase(
    id=401,
    merchant_id=1,
    customer_user_id=1,
    scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=999.0,
    status=CaseStatus.ESCALATED,
    ladder_step=0,
    classification="issuer_decline",
    escalated_to_human=True,
    contact_touches=0
)
db.add(case_401)
db.commit()
db.close()

# Target session_401 with contact
res_sess = client.post("/api/merchant/priority/session_401/contact")
check("session_401 contact returns 200", res_sess.status_code == 200)

db = TestingSessionLocal()
# Ensure case 401 was NOT modified by session_401 contact
case_401_check = db.query(RecoveryCase).filter(RecoveryCase.id == 401).first()
check("case 401 remains ESCALATED when session 401 is targeted", case_401_check.escalated_to_human is True)

# Now target case_401 explicitly
res_case = client.post("/api/merchant/priority/case_401/contact")
check("case_401 contact returns 200", res_case.status_code == 200)
db.expire(case_401_check)
case_401_resolved = db.query(RecoveryCase).filter(RecoveryCase.id == 401).first()
check("case 401 now resolved when case_401 is targeted", case_401_resolved.escalated_to_human is False)
db.close()

print(f"\n{'='*70}")
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  ❌ {f}")
    sys.exit(1)
else:
    print("RESULT: ALL MULTI-SCENARIO PRIORITY QUEUE TESTS PASSED ✅")
print("=" * 70)
