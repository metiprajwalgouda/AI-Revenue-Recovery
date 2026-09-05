"""
Test suite for Merchant Payment Failures Dashboard & Audit Trail
=================================================================
Tests:
  1. HTML page route GET /merchant/payment-failures (200 OK)
  2. API GET /api/merchant/payment-failures returns summary metrics & cases
  3. API GET /api/merchant/payment-failures/{case_id}/audit-trail returns chronological logs
  4. API POST /api/merchant/recovery-actions/{log_id}/approve updates status
  5. API POST /api/merchant/recovery-actions/{log_id}/reject updates status
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
    Base, MerchantUser, CustomerUser, CheckoutSession,
    RecoveryCase, RecoveryActionLog, RecoveryScenario, CaseStatus, ClassificationMethod
)
from app.merchant_auth_routes import get_current_merchant
from app.main import app

# Set up in-memory DB for tests
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

# Seed test merchant & customer
db = TestingSessionLocal()
merchant = MerchantUser(
    id=1, email="merchant@example.com", password_hash="hash",
    store_name="Demo Store", max_discount_pct=15
)
db.add(merchant)

customer = CustomerUser(
    id=1, email="cust@example.com", name="Alice Wonderland",
    phone="+919876543210", password_hash="hash"
)
db.add(customer)
db.commit()

# Seed Payment Failure recovery cases
case1 = RecoveryCase(
    id=101,
    merchant_id=1,
    customer_user_id=1,
    scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=2500.0,
    amount_recovered=0.0,
    status=CaseStatus.INTERVENING,
    ladder_step=1,
    classification="needs_alternate_method",
    classification_source=ClassificationMethod.RULE,
    error_source="bank",
    rar_score=75.0,
    confidence=1.0,
    escalated_to_human=False,
    contact_touches=1,
    last_action_at=datetime.now(timezone.utc),
)
db.add(case1)

case2 = RecoveryCase(
    id=102,
    merchant_id=1,
    customer_user_id=1,
    scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=4500.0,
    amount_recovered=4500.0,
    status=CaseStatus.RECOVERED,
    ladder_step=1,
    classification="retryable_technical",
    classification_source=ClassificationMethod.RULE,
    error_source="gateway",
    rar_score=60.0,
    confidence=1.0,
    escalated_to_human=False,
    contact_touches=1,
    last_action_at=datetime.now(timezone.utc),
)
db.add(case2)

case3 = RecoveryCase(
    id=103,
    merchant_id=1,
    customer_user_id=1,
    scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=10000.0,
    amount_recovered=0.0,
    status=CaseStatus.ESCALATED,
    ladder_step=1,
    classification="risk_terminal",
    classification_source=ClassificationMethod.RULE,
    error_source="business",
    rar_score=100.0,
    confidence=1.0,
    escalated_to_human=True,
    escalation_reason="risk_block",
    contact_touches=0,
    last_action_at=datetime.now(timezone.utc),
)
db.add(case3)

case4 = RecoveryCase(
    id=104,
    merchant_id=1,
    customer_user_id=1,
    scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=1500.0,
    amount_recovered=0.0,
    status=CaseStatus.LOST,
    ladder_step=3,
    classification="needs_alternate_method",
    classification_source=ClassificationMethod.RULE,
    error_source="bank",
    rar_score=75.0,
    confidence=1.0,
    escalated_to_human=False,
    contact_touches=3,
    last_action_at=datetime.now(timezone.utc),
)
db.add(case4)

# Seed RecoveryActionLogs for case 101
log1 = RecoveryActionLog(
    id=201,
    case_id=101,
    idempotency_key="101:1:payment_link_sent",
    ladder_step=1,
    action_type="payment_link_sent",
    reason="Issuing bank declined: insufficient funds. Offer alternate payment link.",
    guardrail_checks=json.dumps({"circuit_breaker_limit": 3, "discount_allowed": False}),
    outcome="sent",
    amount_offered=2500.0,
    requires_human_approval=False,
)
db.add(log1)

log2 = RecoveryActionLog(
    id=202,
    case_id=101,
    idempotency_key="101:2:discount_pending_approval",
    ladder_step=2,
    action_type="discount_offer",
    reason="Proposed 10% discount requires merchant approval.",
    guardrail_checks=json.dumps({"max_discount_cap": 15, "approval_required": True}),
    outcome="pending_approval",
    amount_offered=2250.0,
    requires_human_approval=True,
)
db.add(log2)

db.commit()
db.close()

def override_get_current_merchant(db: Session = Depends(get_db)):
    return db.query(MerchantUser).filter(MerchantUser.id == 1).first()

app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[get_current_merchant] = override_get_current_merchant

from app.auth import create_merchant_session_token, MERCHANT_COOKIE_NAME

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
print("Merchant Payment Failures Dashboard — Test Suite")
print("=" * 70)

# [1] Test HTML Page
print("\n[1] GET /merchant/payment-failures (HTML Page)")
res = client.get("/merchant/payment-failures")
check("status_code == 200", res.status_code == 200, res.status_code)
check("contains 'Payment Failures' title", "Payment Failures" in res.text)
check("contains table headers", "Classification" in res.text and "Ladder Step" in res.text)
check("contains KPI summary placeholders", "kpi-at-risk" in res.text and "kpi-recovered" in res.text)

# [2] Test API GET /api/merchant/payment-failures
print("\n[2] GET /api/merchant/payment-failures (Summary & Cases API)")
res = client.get("/api/merchant/payment-failures")
check("status_code == 200", res.status_code == 200, res.status_code)
data = res.json()
summary = data.get("summary", {})
check("total_cases == 4", summary.get("total_cases") == 4, summary.get("total_cases"))
# total_at_risk: case 101 (2500) + case 103 (10000) = 12500 (102 recovered, 104 lost excluded)
check("total_at_risk == 12500.0", summary.get("total_at_risk") == 12500.0, summary.get("total_at_risk"))
check("total_recovered == 4500.0", summary.get("total_recovered") == 4500.0, summary.get("total_recovered"))
check("recovery_rate_pct == 25.0", summary.get("recovery_rate_pct") == 25.0, summary.get("recovery_rate_pct"))
check("escalated_to_human_count == 1", summary.get("escalated_to_human_count") == 1, summary.get("escalated_to_human_count"))
check("circuit_breaker_trips == 1", summary.get("circuit_breaker_trips") == 1, summary.get("circuit_breaker_trips"))

cases = data.get("cases", [])
check("cases count == 4", len(cases) == 4, len(cases))
case_101 = next((c for c in cases if c["id"] == 101), None)
check("case 101 customer_name == 'Alice Wonderland'", case_101 and case_101["customer_name"] == "Alice Wonderland")
check("case 101 error_source == 'bank'", case_101 and case_101["error_source"] == "bank")
check("case 101 status == 'intervening'", case_101 and case_101["status"] == "intervening")

# [3] Test Audit Trail API
print("\n[3] GET /api/merchant/payment-failures/101/audit-trail")
res = client.get("/api/merchant/payment-failures/101/audit-trail")
check("status_code == 200", res.status_code == 200, res.status_code)
trail = res.json()
check("trail case_id == 101", trail.get("case_id") == 101)
logs = trail.get("logs", [])
check("logs count == 2", len(logs) == 2, len(logs))
check("log 1 action_type == 'payment_link_sent'", logs[0]["action_type"] == "payment_link_sent")
check("log 1 verbatim reason matches", "insufficient funds" in logs[0]["reason"])
check("log 1 guardrail_checks parsed as dict", isinstance(logs[0]["guardrail_checks"], dict))
check("log 2 outcome == 'pending_approval'", logs[1]["outcome"] == "pending_approval")

# [4] Test Approve Endpoint
print("\n[4] POST /api/merchant/recovery-actions/202/approve")
res = client.post("/api/merchant/recovery-actions/202/approve")
check("approve status_code == 200", res.status_code == 200, res.status_code)
check("approve outcome == 'approved'", res.json().get("outcome") == "approved")

# Verify in DB
res = client.get("/api/merchant/payment-failures/101/audit-trail")
updated_log = res.json()["logs"][1]
check("log outcome now 'approved' in trail", updated_log["outcome"] == "approved")
check("log approved_by set", updated_log["approved_by"] == 1)

# [5] Test Reject Endpoint
# Seed a pending log for case 104
db = TestingSessionLocal()
log_rej = RecoveryActionLog(
    id=203,
    case_id=104,
    idempotency_key="104:1:pending",
    ladder_step=1,
    action_type="discount_offer",
    reason="To be rejected",
    guardrail_checks="{}",
    outcome="pending_approval",
    requires_human_approval=True
)
db.add(log_rej)
db.commit()
db.close()

print("\n[5] POST /api/merchant/recovery-actions/203/reject")
res = client.post("/api/merchant/recovery-actions/203/reject")
check("reject status_code == 200", res.status_code == 200, res.status_code)
check("reject outcome == 'rejected'", res.json().get("outcome") == "rejected")

# [6] Double-approve guard: approving an already-approved log must fail with 400
# log 202 was approved in test [4]; trying to approve it again must be rejected.
print("\n[6] Double-approve guard: POST /api/merchant/recovery-actions/202/approve (already approved)")
res = client.post("/api/merchant/recovery-actions/202/approve")
check(
    "double-approve returns 400",
    res.status_code == 400,
    f"got {res.status_code}: {res.text}"
)
check(
    "double-approve error mentions current outcome",
    "approved" in res.json().get("detail", "").lower(),
    res.json().get("detail", "")
)

# [7] Cross-merchant ownership guard: a second merchant must NOT be able to approve
# a log that belongs to merchant 1's case.
# Seed: merchant 2 with their own case and a pending log
db = TestingSessionLocal()
merchant2 = MerchantUser(
    id=2, email="other@example.com", password_hash="hash",
    store_name="Other Store", max_discount_pct=10
)
db.add(merchant2)

case_m2 = RecoveryCase(
    id=201,
    merchant_id=2,           # owned by merchant 2
    customer_user_id=1,
    scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=999.0,
    amount_recovered=0.0,
    status=CaseStatus.AT_RISK,
    ladder_step=1,
    classification="needs_alternate_method",
    classification_source=ClassificationMethod.RULE,
    error_source="bank",
    rar_score=50.0,
    confidence=1.0,
    escalated_to_human=False,
    contact_touches=0,
)
db.add(case_m2)

log_m2 = RecoveryActionLog(
    id=301,
    case_id=201,              # case owned by merchant 2
    idempotency_key="201:1:discount_pending_approval",
    ladder_step=1,
    action_type="discount_offer",
    reason="Merchant 2 pending log",
    guardrail_checks="{}",
    outcome="pending_approval",
    requires_human_approval=True,
)
db.add(log_m2)
db.commit()
db.close()

# The existing test client is authenticated as merchant 1.
# Attempting to approve log 301 (owned by merchant 2's case) must fail 403.
print("\n[7] Cross-merchant guard: POST /api/merchant/recovery-actions/301/approve (wrong merchant)")
res = client.post("/api/merchant/recovery-actions/301/approve")
check(
    "cross-merchant approve returns 403",
    res.status_code == 403,
    f"got {res.status_code}: {res.text}"
)
check(
    "cross-merchant error mentions permission",
    "permission" in res.json().get("detail", "").lower(),
    res.json().get("detail", "")
)

# Also confirm the log was NOT modified (still pending_approval)
db = TestingSessionLocal()
log_check = db.query(RecoveryActionLog).filter(RecoveryActionLog.id == 301).first()
check(
    "cross-merchant: log still pending_approval after failed attempt",
    log_check is not None and log_check.outcome == "pending_approval",
    log_check.outcome if log_check else "log not found"
)
db.close()

# Summary
print(f"\n{'='*70}")
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  ❌ {f}")
    sys.exit(1)
else:
    print("RESULT: ALL DASHBOARD & AUDIT TESTS PASSED ✅")
print("=" * 70)

