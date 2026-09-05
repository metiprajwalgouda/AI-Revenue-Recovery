"""
Test Suite: Mark as Lost in Priority Queue & Total Lost KPI Metrics
===================================================================
Verifies:
  1. POST /api/merchant/priority/{target_id}/lost marks an escalated RecoveryCase as LOST:
     - case.status becomes CaseStatus.LOST
     - case.escalated_to_human becomes False
     - case.next_action_due_at is cleared
     - A RecoveryActionLog is written with action_type="human_marked_lost", outcome="sent",
       requires_human_approval=False, reason matching input note.
  2. The marked-lost case disappears from GET /api/merchant/priority.
  3. GET /api/merchant/payment-failures reflects total_lost and lost_cases correctly.
  4. GET /api/merchant/advanced-metrics reflects lost (sum) and lost_count per scenario.
  5. Cross-merchant guard: merchant cannot mark another merchant's case as lost (403).
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
    ClassificationMethod,
)
from app.merchant_auth_routes import get_current_merchant
from app.auth import create_merchant_session_token, MERCHANT_COOKIE_NAME
from app.main import app

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

m1 = MerchantUser(id=1, email="m1@example.com", password_hash="hash", store_name="Store One", max_discount_pct=15)
m2 = MerchantUser(id=2, email="m2@example.com", password_hash="hash", store_name="Store Two", max_discount_pct=15)
db.add_all([m1, m2])

cust = CustomerUser(id=1, email="cust@example.com", name="Bob Customer", phone="+919876543210", password_hash="hash")
db.add(cust)

# Sessions
s1 = CheckoutSession(
    id=101, event_id="evt_lost_pf_01", customer_user_id=1,
    customer_name="Bob Customer", customer_email="cust@example.com", customer_phone="+919876543210",
    cart_value=3500.0, final_amount_charged=3500.0, cart_json=json.dumps([]), status=SessionStatus.ABANDONED,
)
s2 = CheckoutSession(
    id=102, event_id="evt_lost_ab_01", customer_user_id=1,
    customer_name="Bob Customer", customer_email="cust@example.com", customer_phone="+919876543210",
    cart_value=2000.0, final_amount_charged=2000.0, cart_json=json.dumps([]), status=SessionStatus.ABANDONED,
)
s_m2 = CheckoutSession(
    id=103, event_id="evt_m2_pf_01", customer_user_id=1,
    customer_name="Bob Customer", customer_email="cust@example.com", customer_phone="+919876543210",
    cart_value=5000.0, final_amount_charged=5000.0, cart_json=json.dumps([]), status=SessionStatus.ABANDONED,
)
db.add_all([s1, s2, s_m2])

# Case 1: Merchant 1, Payment Failure, Escalated to human
case1 = RecoveryCase(
    id=201, merchant_id=1, customer_user_id=1, checkout_session_id=101,
    scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=3500.0, amount_recovered=0.0,
    status=CaseStatus.ESCALATED, ladder_step=2,
    classification="risk_terminal", classification_source=ClassificationMethod.RULE,
    error_source="business", rar_score=100.0, confidence=1.0,
    escalated_to_human=True, escalation_reason="risk_block",
    contact_touches=1, last_action_at=datetime.now(timezone.utc),
    next_action_due_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
)

# Case 2: Merchant 1, Checkout Abandonment, Escalated to human
case2 = RecoveryCase(
    id=202, merchant_id=1, customer_user_id=1, checkout_session_id=102,
    scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
    amount_at_risk=2000.0, amount_recovered=0.0,
    status=CaseStatus.ESCALATED, ladder_step=1,
    classification="price_sensitive", classification_source=ClassificationMethod.LLM,
    error_source=None, rar_score=60.0, confidence=0.8,
    escalated_to_human=True, escalation_reason="high_value",
    contact_touches=1, last_action_at=datetime.now(timezone.utc),
)

# Case 3: Merchant 2 case (for cross-merchant check)
case_m2 = RecoveryCase(
    id=203, merchant_id=2, customer_user_id=1, checkout_session_id=103,
    scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=5000.0, amount_recovered=0.0,
    status=CaseStatus.ESCALATED, ladder_step=1,
    classification="risk_terminal", classification_source=ClassificationMethod.RULE,
    error_source="business", rar_score=100.0, confidence=1.0,
    escalated_to_human=True, escalation_reason="risk_block",
    contact_touches=1, last_action_at=datetime.now(timezone.utc),
)

db.add_all([case1, case2, case_m2])
db.commit()
db.close()

# ── Auth overrides ────────────────────────────────────────────────────────────
def override_get_current_merchant(db: Session = Depends(override_get_db)):
    return db.query(MerchantUser).filter(MerchantUser.id == 1).first()

app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[get_current_merchant] = override_get_current_merchant

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
print("Mark as Lost & Total Lost KPI Tests")
print("=" * 70)

# ── [1] Priority queue initially has both cases ───────────────────────────────
print("\n[1] Priority queue initial state")
res = client.get("/api/merchant/priority")
check("status 200", res.status_code == 200, res.status_code)
items = res.json()
case_ids = {it.get("case_id") for it in items}
check("case 201 present in priority queue", 201 in case_ids, case_ids)
check("case 202 present in priority queue", 202 in case_ids, case_ids)

# ── [2] Mark Case 201 as Lost via POST /api/merchant/priority/case_201/lost ───
print("\n[2] POST /api/merchant/priority/case_201/lost")
res = client.post(
    "/api/merchant/priority/case_201/lost",
    json={"reason": "Customer refused alternate card, confirmed lost"}
)
check("status 200", res.status_code == 200, res.status_code)

db = TestingSessionLocal()
c1 = db.query(RecoveryCase).filter(RecoveryCase.id == 201).first()
check("c1.status == CaseStatus.LOST", c1.status == CaseStatus.LOST, c1.status)
check("c1.escalated_to_human == False", c1.escalated_to_human is False, c1.escalated_to_human)
check("c1.next_action_due_at is None", c1.next_action_due_at is None, c1.next_action_due_at)

logs = db.query(RecoveryActionLog).filter(
    RecoveryActionLog.case_id == 201,
    RecoveryActionLog.action_type == "human_marked_lost"
).all()
check("exactly 1 human_marked_lost log row", len(logs) == 1, len(logs))
if logs:
    log = logs[0]
    check("log.outcome == 'sent'", log.outcome == "sent", log.outcome)
    check("log.requires_human_approval == False", log.requires_human_approval is False, log.requires_human_approval)
    check("log.reason recorded correctly", "Customer refused alternate card" in log.reason, log.reason)
    check("log.approved_by == 1", log.approved_by == 1, log.approved_by)
db.close()

# ── [3] Priority queue: Case 201 must now be gone ────────────────────────────
print("\n[3] Priority queue after mark-as-lost")
res = client.get("/api/merchant/priority")
check("status 200", res.status_code == 200, res.status_code)
items = res.json()
case_ids = {it.get("case_id") for it in items}
check("case 201 is now ABSENT from priority queue", 201 not in case_ids, case_ids)
check("case 202 is still present in priority queue", 202 in case_ids, case_ids)

# ── [4] Payment Failures Dashboard: Total Lost KPI ───────────────────────────
print("\n[4] GET /api/merchant/payment-failures KPI summary")
res = client.get("/api/merchant/payment-failures")
check("status 200", res.status_code == 200, res.status_code)
pf_data = res.json()
summary = pf_data.get("summary", {})
check("total_lost == 3500.0", summary.get("total_lost") == 3500.0, summary.get("total_lost"))
check("lost_cases == 1", summary.get("lost_cases") == 1, summary.get("lost_cases"))
check("total_at_risk == 0.0 (since lost case is not active at risk)", summary.get("total_at_risk") == 0.0, summary.get("total_at_risk"))

# ── [5] Advanced Metrics: Scenario breakdown lost metrics ─────────────────────
print("\n[5] GET /api/merchant/advanced-metrics lost breakdown")
res = client.get("/api/merchant/advanced-metrics")
check("status 200", res.status_code == 200, res.status_code)
metrics = res.json()

pf_block = metrics.get("payment_failure", {})
ab_block = metrics.get("checkout_abandonment", {})
ov_block = metrics.get("overall", {})

check("pf.lost == 3500.0", pf_block.get("lost") == 3500.0, pf_block.get("lost"))
check("pf.lost_count == 1", pf_block.get("lost_count") == 1, pf_block.get("lost_count"))
check("ab.lost == 0.0", ab_block.get("lost") == 0.0, ab_block.get("lost"))
check("ab.lost_count == 0", ab_block.get("lost_count") == 0, ab_block.get("lost_count"))
check("overall.total_lost == 3500.0", ov_block.get("total_lost") == 3500.0, ov_block.get("total_lost"))

# ── [6] Cross-merchant guard: Merchant 1 cannot mark Merchant 2 case as lost ───
print("\n[6] Cross-merchant authorization guard")
res = client.post("/api/merchant/priority/case_203/lost", json={"reason": "Unauthorized attempt"})
check("cross-merchant returns 403", res.status_code == 403, res.status_code)

db = TestingSessionLocal()
c_m2 = db.query(RecoveryCase).filter(RecoveryCase.id == 203).first()
check("merchant 2 case status remains ESCALATED", c_m2.status == CaseStatus.ESCALATED, c_m2.status)
db.close()

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'=' * 70}")
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  ❌ {f}")
    sys.exit(1)
else:
    print("RESULT: ALL MARK-AS-LOST & KPI TESTS PASSED ✅")
print("=" * 70)
