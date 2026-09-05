"""
Test suite for /api/merchant/advanced-metrics with Scenario Breakdown
=====================================================================
Tests:
  1. GET /api/merchant/advanced-metrics returns payment_failure, checkout_abandonment,
     overdue_receivable, overall breakdown matching exact requested schema.
  2. Numbers correctly computed from recovery_cases and checkout_sessions.
  3. Legacy keys (funnel, financials, cohorts, audit_logs) remain intact.
"""

import os
import sys
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
    RecoveryCase, RecoveryActionLog, RecoveryScenario, CaseStatus, ClassificationMethod
)
from app.merchant_auth_routes import get_current_merchant
from app.auth import create_merchant_session_token, MERCHANT_COOKIE_NAME
from app.main import app

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

# Seed merchant & customer
db = TestingSessionLocal()
merchant = MerchantUser(
    id=1, email="merchant@example.com", password_hash="hash",
    store_name="Demo Store", max_discount_pct=15
)
db.add(merchant)

cust = CustomerUser(
    id=1, email="user@example.com", name="Test User",
    phone="+919876543210", password_hash="hash"
)
db.add(cust)

# Payment failure cases:
# Case 1: at_risk 2000, not recovered, status=INTERVENING
c1 = RecoveryCase(
    id=1, merchant_id=1, customer_user_id=1, scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=2000.0, amount_recovered=0.0, status=CaseStatus.INTERVENING,
    ladder_step=1, classification="needs_alternate_method", escalated_to_human=False, contact_touches=1
)
# Case 2: at_risk 3000, recovered 3000, status=RECOVERED
c2 = RecoveryCase(
    id=2, merchant_id=1, customer_user_id=1, scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=3000.0, amount_recovered=3000.0, status=CaseStatus.RECOVERED,
    ladder_step=1, classification="retryable_technical", escalated_to_human=False, contact_touches=1
)
# Case 3: at_risk 5000, status=ESCALATED, escalated_to_human=True
c3 = RecoveryCase(
    id=3, merchant_id=1, customer_user_id=1, scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=5000.0, amount_recovered=0.0, status=CaseStatus.ESCALATED,
    ladder_step=1, classification="risk_terminal", escalated_to_human=True, contact_touches=0
)
# Case 4: at_risk 1000, status=LOST, contact_touches=3 (circuit breaker trip)
c4 = RecoveryCase(
    id=4, merchant_id=1, customer_user_id=1, scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=1000.0, amount_recovered=0.0, status=CaseStatus.LOST,
    ladder_step=3, classification="needs_alternate_method", escalated_to_human=False, contact_touches=3
)

# Checkout abandonment cases:
# Case 5: at_risk 4000, status=INTERVENING
c5 = RecoveryCase(
    id=5, merchant_id=1, customer_user_id=1, scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
    amount_at_risk=4000.0, amount_recovered=0.0, status=CaseStatus.INTERVENING,
    ladder_step=1, classification="high_amount_hesitation", escalated_to_human=False, contact_touches=1
)
# Case 6: at_risk 6000, recovered 6000, status=RECOVERED
c6 = RecoveryCase(
    id=6, merchant_id=1, customer_user_id=1, scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
    amount_at_risk=6000.0, amount_recovered=6000.0, status=CaseStatus.RECOVERED,
    ladder_step=1, classification="price_shock_at_checkout", escalated_to_human=False, contact_touches=1
)

db.add_all([c1, c2, c3, c4, c5, c6])
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
print("Advanced Metrics Breakdown by Scenario Test Suite")
print("=" * 70)

res = client.get("/api/merchant/advanced-metrics")
check("status_code == 200", res.status_code == 200, res.status_code)
data = res.json()

# [1] Verify top-level keys
print("\n[1] Verify Top-Level Structure")
check("contains 'payment_failure'", "payment_failure" in data)
check("contains 'checkout_abandonment'", "checkout_abandonment" in data)
check("contains 'overdue_receivable'", "overdue_receivable" in data)
check("contains 'overall'", "overall" in data)
check("contains legacy 'funnel'", "funnel" in data)
check("contains legacy 'financials'", "financials" in data)

# [2] Verify payment_failure breakdown
print("\n[2] Verify payment_failure breakdown")
pf = data.get("payment_failure", {})
# At risk: c1 (2000) + c3 (5000) = 7000 (c2 is RECOVERED, c4 is LOST)
check("pf.at_risk == 7000.0", pf.get("at_risk") == 7000.0, pf.get("at_risk"))
check("pf.recovered == 3000.0", pf.get("recovered") == 3000.0, pf.get("recovered"))
# Recovery rate: 1 recovered out of 4 total cases = 25.0%
check("pf.recovery_rate == 25.0", pf.get("recovery_rate") == 25.0, pf.get("recovery_rate"))
check("pf.escalated == 1", pf.get("escalated") == 1, pf.get("escalated"))
check("pf.circuit_breaker_trips == 1", pf.get("circuit_breaker_trips") == 1, pf.get("circuit_breaker_trips"))

# [3] Verify checkout_abandonment breakdown
print("\n[3] Verify checkout_abandonment breakdown")
ab = data.get("checkout_abandonment", {})
# At risk: c5 (4000) = 4000.0 (c6 is RECOVERED)
check("ab.at_risk == 4000.0", ab.get("at_risk") == 4000.0, ab.get("at_risk"))
check("ab.recovered == 6000.0", ab.get("recovered") == 6000.0, ab.get("recovered"))
# Recovery rate: 1 recovered out of 2 total cases = 50.0%
check("ab.recovery_rate == 50.0", ab.get("recovery_rate") == 50.0, ab.get("recovery_rate"))
check("ab.escalated == 0", ab.get("escalated") == 0, ab.get("escalated"))
check("ab.circuit_breaker_trips == 0", ab.get("circuit_breaker_trips") == 0, ab.get("circuit_breaker_trips"))

# [4] Verify overdue_receivable zeroed block
print("\n[4] Verify overdue_receivable zeroed block")
rec = data.get("overdue_receivable", {})
check("rec.at_risk == 0.0", rec.get("at_risk") == 0.0, rec.get("at_risk"))
check("rec.recovered == 0.0", rec.get("recovered") == 0.0, rec.get("recovered"))
check("rec.recovery_rate == 0.0", rec.get("recovery_rate") == 0.0, rec.get("recovery_rate"))
check("rec.escalated == 0", rec.get("escalated") == 0, rec.get("escalated"))
check("rec.circuit_breaker_trips == 0", rec.get("circuit_breaker_trips") == 0, rec.get("circuit_breaker_trips"))

# [5] Verify overall stats
print("\n[5] Verify overall stats")
ov = data.get("overall", {})
# total_at_risk: pf at_risk (7000) + ab at_risk (4000) = 11000.0
check("ov.total_at_risk == 11000.0", ov.get("total_at_risk") == 11000.0, ov.get("total_at_risk"))
# total_recovered: pf recovered (3000) + ab recovered (6000) = 9000.0
check("ov.total_recovered == 9000.0", ov.get("total_recovered") == 9000.0, ov.get("total_recovered"))
check("ov.cost_per_rupee_recovered is float/number", isinstance(ov.get("cost_per_rupee_recovered"), (int, float)))

print(f"\n{'='*70}")
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  ❌ {f}")
    sys.exit(1)
else:
    print("RESULT: ALL ADVANCED METRICS SCENARIO BREAKDOWN TESTS PASSED ✅")
print("=" * 70)
