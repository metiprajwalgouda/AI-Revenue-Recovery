"""
Merchant Data Isolation Test
============================
Verifies that the three dashboard endpoints that query RecoveryCase never
leak data across merchant boundaries.

Scenario:
  - merchant_id=2 has 2 RecoveryCase rows (1 PAYMENT_FAILURE, 1 CHECKOUT_ABANDONMENT,
    1 escalated to human).
  - merchant_id=3 has 2 RecoveryCase rows (1 PAYMENT_FAILURE, 1 CHECKOUT_ABANDONMENT).

We log in as merchant_id=2 and assert:
  - /api/merchant/priority      → only escalated case for merchant 2 appears
  - /api/merchant/payment-failures → only merchant 2's PAYMENT_FAILURE case appears
  - /api/merchant/advanced-metrics → only merchant 2's totals appear (at_risk, recovered)

merchant_id=3's IDs and amounts must NEVER appear in any of the three responses.
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
    RecoveryCase, RecoveryActionLog, RecoveryScenario, CaseStatus, ClassificationMethod,
    SessionStatus,
)
from app.merchant_auth_routes import get_current_merchant
from app.main import app
from app.auth import create_merchant_session_token, MERCHANT_COOKIE_NAME

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

# Two merchants
m2 = MerchantUser(id=2, email="m2@example.com", password_hash="hash",
                  store_name="Merchant Two Store", max_discount_pct=10)
m3 = MerchantUser(id=3, email="m3@example.com", password_hash="hash",
                  store_name="Merchant Three Store", max_discount_pct=10)
db.add_all([m2, m3])

customer = CustomerUser(id=1, email="cust@example.com", name="Test Customer",
                        phone="+910000000000", password_hash="hash")
db.add(customer)

# Two minimal CheckoutSession rows so FK constraints are satisfied
sess_m2_pf = CheckoutSession(
    id=501, event_id="evt_iso_m2_pf", customer_user_id=1,
    customer_name="Test Customer", customer_email="cust@example.com",
    customer_phone="+910000000000", cart_value=1000.0,
    final_amount_charged=1000.0, cart_json=json.dumps([]),
    status=SessionStatus.ABANDONED,
)
sess_m2_ab = CheckoutSession(
    id=502, event_id="evt_iso_m2_ab", customer_user_id=1,
    customer_name="Test Customer", customer_email="cust@example.com",
    customer_phone="+910000000000", cart_value=2000.0,
    final_amount_charged=2000.0, cart_json=json.dumps([]),
    status=SessionStatus.ABANDONED,
)
sess_m3_pf = CheckoutSession(
    id=503, event_id="evt_iso_m3_pf", customer_user_id=1,
    customer_name="Test Customer", customer_email="cust@example.com",
    customer_phone="+910000000000", cart_value=9999.0,   # ← sentinel amount
    final_amount_charged=9999.0, cart_json=json.dumps([]),
    status=SessionStatus.ABANDONED,
)
sess_m3_ab = CheckoutSession(
    id=504, event_id="evt_iso_m3_ab", customer_user_id=1,
    customer_name="Test Customer", customer_email="cust@example.com",
    customer_phone="+910000000000", cart_value=8888.0,   # ← sentinel amount
    final_amount_charged=8888.0, cart_json=json.dumps([]),
    status=SessionStatus.ABANDONED,
)
db.add_all([sess_m2_pf, sess_m2_ab, sess_m3_pf, sess_m3_ab])

# ── Merchant 2 cases ──────────────────────────────────────────────────────────
# PAYMENT_FAILURE — at_risk=1000, not escalated
case_m2_pf = RecoveryCase(
    id=601, merchant_id=2, customer_user_id=1, checkout_session_id=501,
    scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=1000.0, amount_recovered=0.0,
    status=CaseStatus.INTERVENING,
    ladder_step=1,
    classification="needs_alternate_method",
    classification_source=ClassificationMethod.RULE,
    error_source="bank", rar_score=75.0, confidence=1.0,
    escalated_to_human=False, contact_touches=1,
    last_action_at=datetime.now(timezone.utc),
)
# CHECKOUT_ABANDONMENT — escalated to human (must appear in priority queue for m2)
case_m2_ab = RecoveryCase(
    id=602, merchant_id=2, customer_user_id=1, checkout_session_id=502,
    scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
    amount_at_risk=2000.0, amount_recovered=0.0,
    status=CaseStatus.ESCALATED,
    ladder_step=2,
    classification="price_sensitive",
    classification_source=ClassificationMethod.LLM,
    error_source=None, rar_score=60.0, confidence=0.8,
    escalated_to_human=True, escalation_reason="high_value",
    contact_touches=2,
    last_action_at=datetime.now(timezone.utc),
)
db.add_all([case_m2_pf, case_m2_ab])

# ── Merchant 3 cases — must NEVER appear in merchant 2's views ────────────────
# PAYMENT_FAILURE — sentinel at_risk=9999
case_m3_pf = RecoveryCase(
    id=701, merchant_id=3, customer_user_id=1, checkout_session_id=503,
    scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=9999.0, amount_recovered=0.0,
    status=CaseStatus.INTERVENING,
    ladder_step=1,
    classification="retryable_technical",
    classification_source=ClassificationMethod.RULE,
    error_source="gateway", rar_score=60.0, confidence=1.0,
    escalated_to_human=False, contact_touches=1,
    last_action_at=datetime.now(timezone.utc),
)
# CHECKOUT_ABANDONMENT — escalated, sentinel at_risk=8888
case_m3_ab = RecoveryCase(
    id=702, merchant_id=3, customer_user_id=1, checkout_session_id=504,
    scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
    amount_at_risk=8888.0, amount_recovered=0.0,
    status=CaseStatus.ESCALATED,
    ladder_step=1,
    classification="price_sensitive",
    classification_source=ClassificationMethod.LLM,
    error_source=None, rar_score=55.0, confidence=0.7,
    escalated_to_human=True, escalation_reason="repeated_abandon",
    contact_touches=1,
    last_action_at=datetime.now(timezone.utc),
)
db.add_all([case_m3_pf, case_m3_ab])
db.commit()
db.close()

# ── Override auth — log in as merchant 2 ─────────────────────────────────────
def override_get_current_merchant(db: Session = Depends(get_db)):
    return db.query(MerchantUser).filter(MerchantUser.id == 2).first()

app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[get_current_merchant] = override_get_current_merchant

token = create_merchant_session_token(2)
client = TestClient(app, cookies={MERCHANT_COOKIE_NAME: token})

# ── Helpers ───────────────────────────────────────────────────────────────────
PASS = "✅ PASS"
FAIL = "❌ FAIL"
failures = []

def check(name, condition, detail=""):
    if condition:
        print(f"  {PASS}  {name}")
    else:
        print(f"  {FAIL}  {name}  ({detail})")
        failures.append(f"{name}: {detail}")

SENTINEL_AMOUNTS = {9999.0, 8888.0}
SENTINEL_IDS = {701, 702}

# ── Run tests ─────────────────────────────────────────────────────────────────
print("=" * 70)
print("Merchant Data Isolation Test (logged in as merchant_id=2)")
print("=" * 70)

# ── [1] /api/merchant/priority ────────────────────────────────────────────────
print("\n[1] GET /api/merchant/priority — must only show merchant 2's escalated case")
res = client.get("/api/merchant/priority")
check("status 200", res.status_code == 200, res.status_code)
data = res.json()

# Collect all IDs and amounts that appear in priority items
priority_items = data if isinstance(data, list) else data.get("items", data.get("sessions", []))
priority_case_ids = {item.get("case_id") for item in priority_items}
priority_amounts  = {item.get("cart_value") for item in priority_items}

check(
    "merchant 2's escalated case (id=602) is present",
    602 in priority_case_ids,
    f"case_ids in response: {priority_case_ids}",
)
check(
    "merchant 3's escalated case (id=702) is ABSENT",
    702 not in priority_case_ids,
    f"case_ids in response: {priority_case_ids}",
)
check(
    "no sentinel amount from merchant 3 (8888/9999) appears in priority amounts",
    not (SENTINEL_AMOUNTS & priority_amounts),
    f"amounts in response: {priority_amounts}",
)

# ── [2] /api/merchant/payment-failures ───────────────────────────────────────
print("\n[2] GET /api/merchant/payment-failures — must only show merchant 2's PF case")
res = client.get("/api/merchant/payment-failures")
check("status 200", res.status_code == 200, res.status_code)
pf_data = res.json()
pf_cases = pf_data.get("cases", [])
pf_case_ids   = {c.get("id") for c in pf_cases}
pf_amounts    = {c.get("amount_at_risk") for c in pf_cases}
pf_total      = pf_data.get("summary", {}).get("total_at_risk", 0)

check(
    "merchant 2's PF case (id=601) is present",
    601 in pf_case_ids,
    f"case_ids: {pf_case_ids}",
)
check(
    "merchant 3's PF case (id=701) is ABSENT",
    701 not in pf_case_ids,
    f"case_ids: {pf_case_ids}",
)
check(
    "total_at_risk == 1000 (only merchant 2's case)",
    pf_total == 1000.0,
    f"got {pf_total}",
)
check(
    "no sentinel amount 9999 in per-case amounts",
    9999.0 not in pf_amounts,
    f"amounts: {pf_amounts}",
)

# ── [3] /api/merchant/advanced-metrics ───────────────────────────────────────
print("\n[3] GET /api/merchant/advanced-metrics — totals must only reflect merchant 2")
res = client.get("/api/merchant/advanced-metrics")
check("status 200", res.status_code == 200, res.status_code)
metrics = res.json()

pf_block  = metrics.get("payment_failure", {})
ab_block  = metrics.get("checkout_abandonment", {})
ov_block  = metrics.get("overall", {})

# Merchant 2 has: PF at_risk=1000 (INTERVENING — counts), AB at_risk=2000 (ESCALATED — counts)
# Merchant 3 has: PF at_risk=9999, AB at_risk=8888 — must NOT appear
check(
    "pf.at_risk == 1000.0 (only merchant 2's PF case)",
    pf_block.get("at_risk") == 1000.0,
    f"got {pf_block.get('at_risk')}",
)
check(
    "ab.at_risk == 2000.0 (only merchant 2's AB case)",
    ab_block.get("at_risk") == 2000.0,
    f"got {ab_block.get('at_risk')}",
)
check(
    "overall.total_at_risk == 3000.0 (1000+2000)",
    ov_block.get("total_at_risk") == 3000.0,
    f"got {ov_block.get('total_at_risk')}",
)
check(
    "no sentinel 9999 in pf block",
    pf_block.get("at_risk") != 9999.0,
    f"pf.at_risk={pf_block.get('at_risk')}",
)
check(
    "no sentinel 8888 in ab block",
    ab_block.get("at_risk") != 8888.0,
    f"ab.at_risk={ab_block.get('at_risk')}",
)

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'=' * 70}")
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  ❌ {f}")
    sys.exit(1)
else:
    print("RESULT: ALL ISOLATION CHECKS PASSED ✅")
print("=" * 70)
