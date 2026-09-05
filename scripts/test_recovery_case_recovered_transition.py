"""
RecoveryCase RECOVERED Transition Tests
========================================
Tests that complete_checkout correctly closes out a matching RecoveryCase:

  [1] Payment-failure path: checkout_session_id == session.id
      → RecoveryCase.status becomes RECOVERED
      → amount_recovered set to final_amount_charged
      → next_action_due_at cleared
      → RecoveryActionLog written with action_type="case_recovered"

  [2] Abandonment-recovery path: checkout_session_id == session.recovered_from_session_id
      → new session completed, points back to old (abandoned) session
      → RecoveryCase on the OLD session closes as RECOVERED

  [3] Idempotency guard: calling complete_checkout a second time for the same
      session does NOT create a second RecoveryActionLog row and does NOT crash.

  [4] Already-RECOVERED guard: if the case is already RECOVERED when
      complete_checkout fires (shouldn't happen normally), it is not modified again.
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
from sqlalchemy.orm import sessionmaker

from app.db import get_db
from app.db_models import (
    Base, MerchantUser, CustomerUser, CheckoutSession, SessionStatus,
    RecoveryCase, RecoveryActionLog, RecoveryScenario, CaseStatus,
    ClassificationMethod, RecoveryOutcomeRecord,
)
from app.customer_auth_routes import get_current_customer
from app.razorpay_client import SimulatedRazorpayClient
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

# ── Seed fixtures ─────────────────────────────────────────────────────────────
db = TestingSessionLocal()

merchant = MerchantUser(
    id=1, email="m@example.com", password_hash="hash",
    store_name="Test Store", max_discount_pct=15,
)
customer = CustomerUser(
    id=1, email="cust@example.com", name="Test Customer",
    phone="+910000000000", password_hash="hash",
)
db.add_all([merchant, customer])

# ── [1] Payment-failure path session ─────────────────────────────────────────
# Session on which the payment failure happened AND on which the customer retries.
sess_pf = CheckoutSession(
    id=801, event_id="evt_rc_pf_01",
    customer_user_id=1,
    customer_name="Test Customer", customer_email="cust@example.com",
    customer_phone="+910000000000",
    cart_value=2500.0, final_amount_charged=2500.0,
    cart_json=json.dumps([]),
    status=SessionStatus.ABANDONED,
    recovered_from_session_id=None,
)
db.add(sess_pf)

# RecoveryCase linked to that same session
case_pf = RecoveryCase(
    id=901, merchant_id=1, customer_user_id=1,
    checkout_session_id=801,
    scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=2500.0, amount_recovered=0.0,
    status=CaseStatus.INTERVENING,
    ladder_step=1,
    classification="needs_alternate_method",
    classification_source=ClassificationMethod.RULE,
    error_source="bank", rar_score=75.0, confidence=1.0,
    escalated_to_human=False, contact_touches=1,
    last_action_at=datetime.now(timezone.utc),
    next_action_due_at=datetime(2099, 1, 1, tzinfo=timezone.utc),  # sentinel — must be cleared
)
db.add(case_pf)

# ── [2] Abandonment-recovery path sessions ────────────────────────────────────
# Original abandoned session (old)
sess_ab_orig = CheckoutSession(
    id=802, event_id="evt_rc_ab_orig",
    customer_user_id=1,
    customer_name="Test Customer", customer_email="cust@example.com",
    customer_phone="+910000000000",
    cart_value=4000.0, final_amount_charged=4000.0,
    cart_json=json.dumps([]),
    status=SessionStatus.ABANDONED,
    recovered_from_session_id=None,
)
db.add(sess_ab_orig)

# New session started from the recovery link (points back to orig via recovered_from_session_id)
sess_ab_new = CheckoutSession(
    id=803, event_id="evt_rc_ab_new",
    customer_user_id=1,
    customer_name="Test Customer", customer_email="cust@example.com",
    customer_phone="+910000000000",
    cart_value=4000.0, final_amount_charged=3800.0,   # ← discounted
    cart_json=json.dumps([]),
    status=SessionStatus.ABANDONED,
    recovered_from_session_id=802,   # ← points back to original
)
db.add(sess_ab_new)

# RecoveryCase linked to the ORIGINAL session
case_ab = RecoveryCase(
    id=902, merchant_id=1, customer_user_id=1,
    checkout_session_id=802,   # ← old session
    scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
    amount_at_risk=4000.0, amount_recovered=0.0,
    status=CaseStatus.INTERVENING,
    ladder_step=1,
    classification="price_sensitive",
    classification_source=ClassificationMethod.LLM,
    error_source=None, rar_score=60.0, confidence=0.8,
    escalated_to_human=False, contact_touches=1,
    last_action_at=datetime.now(timezone.utc),
    next_action_due_at=None,
)
db.add(case_ab)

# Add a RecoveryOutcomeRecord for the original session (old table)
outcome_record = RecoveryOutcomeRecord(
    session_id=802,
    predicted_reason="price_sensitive",
    confidence=0.8,
    classification_method="llm",
    reasoning="Customer appeared price sensitive",
    action_taken="email",
    action_success=True,
    amount_offered=3800.0,
)
db.add(outcome_record)

# ── [4] Already-RECOVERED case (must not be touched) ─────────────────────────
sess_already = CheckoutSession(
    id=804, event_id="evt_rc_already",
    customer_user_id=1,
    customer_name="Test Customer", customer_email="cust@example.com",
    customer_phone="+910000000000",
    cart_value=1000.0, final_amount_charged=1000.0,
    cart_json=json.dumps([]),
    status=SessionStatus.ABANDONED,
    recovered_from_session_id=None,
)
db.add(sess_already)

case_already = RecoveryCase(
    id=903, merchant_id=1, customer_user_id=1,
    checkout_session_id=804,
    scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=1000.0, amount_recovered=1000.0,   # ← already RECOVERED
    status=CaseStatus.RECOVERED,
    ladder_step=1, classification="retryable_technical",
    classification_source=ClassificationMethod.RULE,
    error_source="gateway", rar_score=60.0, confidence=1.0,
    escalated_to_human=False, contact_touches=1,
    last_action_at=datetime.now(timezone.utc),
    next_action_due_at=None,
)
db.add(case_already)

db.commit()
db.close()

# ── Auth + Razorpay overrides ─────────────────────────────────────────────────
def fake_customer(db=Depends(override_get_db)):
    return db.query(CustomerUser).filter(CustomerUser.id == 1).first()

def fake_razorpay_client():
    """SimulatedRazorpayClient.verify_payment_signature returns True for any
    signature except 'mock_invalid_signature', so our test payloads pass."""
    return SimulatedRazorpayClient()

app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[get_current_customer] = fake_customer
app.dependency_overrides[get_razorpay_client] = fake_razorpay_client

client = TestClient(app, raise_server_exceptions=True)

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

def call_complete(event_id: str) -> dict:
    """POST /api/checkout/complete with a mock valid payload."""
    return client.post("/api/checkout/complete", json={
        "event_id": event_id,
        "razorpay_order_id": f"order_{event_id}",
        "razorpay_payment_id": f"pay_{event_id}",
        "razorpay_signature": "mock_valid_signature",
    })

# ── [1] Payment-failure path ──────────────────────────────────────────────────
print("=" * 70)
print("RecoveryCase RECOVERED Transition Tests")
print("=" * 70)

print("\n[1] Payment-failure path — complete_checkout closes case on SAME session")
res = call_complete("evt_rc_pf_01")
check("HTTP 200", res.status_code == 200, res.status_code)

db = TestingSessionLocal()
case = db.query(RecoveryCase).filter(RecoveryCase.id == 901).first()
check("case.status == RECOVERED", case.status == CaseStatus.RECOVERED, case.status)
check("case.amount_recovered == 2500.0", case.amount_recovered == 2500.0, case.amount_recovered)
check("case.next_action_due_at is None", case.next_action_due_at is None, case.next_action_due_at)

logs = db.query(RecoveryActionLog).filter(
    RecoveryActionLog.case_id == 901,
    RecoveryActionLog.action_type == "case_recovered",
).all()
check("exactly 1 RecoveryActionLog with action_type='case_recovered'", len(logs) == 1, len(logs))
if logs:
    log = logs[0]
    check("log.outcome == 'sent'", log.outcome == "sent", log.outcome)
    check("log.requires_human_approval == False", log.requires_human_approval is False, log.requires_human_approval)
    guardrail = json.loads(log.guardrail_checks or "{}")
    check("guardrail contains confirmed_payment_id", "confirmed_payment_id" in guardrail, guardrail)
    check("guardrail.amount_recovered == 2500.0", guardrail.get("amount_recovered") == 2500.0, guardrail.get("amount_recovered"))
db.close()

# ── [2] Abandonment-recovery path ─────────────────────────────────────────────
print("\n[2] Abandonment-recovery path — complete_checkout closes case on ORIGINAL session")
res = call_complete("evt_rc_ab_new")
check("HTTP 200", res.status_code == 200, res.status_code)

db = TestingSessionLocal()
case = db.query(RecoveryCase).filter(RecoveryCase.id == 902).first()
check("case.status == RECOVERED", case.status == CaseStatus.RECOVERED, case.status)
# amount_recovered must be final_amount_charged from the NEW session (3800, discounted)
check("case.amount_recovered == 3800.0", case.amount_recovered == 3800.0, case.amount_recovered)

logs = db.query(RecoveryActionLog).filter(
    RecoveryActionLog.case_id == 902,
    RecoveryActionLog.action_type == "case_recovered",
).all()
check("exactly 1 RecoveryActionLog for abandonment case", len(logs) == 1, len(logs))

# Old RecoveryOutcomeRecord must still be updated (existing logic preserved)
outcome = db.query(RecoveryOutcomeRecord).filter(RecoveryOutcomeRecord.session_id == 802).first()
check("RecoveryOutcomeRecord.confirmed_recovered_amount == 3800.0",
      outcome is not None and outcome.confirmed_recovered_amount == 3800.0,
      outcome.confirmed_recovered_amount if outcome else "no record")
db.close()

# ── [3] Idempotency: double-call does NOT create a second log row ──────────────
print("\n[3] Idempotency — second call to complete_checkout must not duplicate audit log")
# Re-seed: we need a fresh session that hasn't been completed yet
db = TestingSessionLocal()
sess_idem = CheckoutSession(
    id=805, event_id="evt_rc_idem",
    customer_user_id=1,
    customer_name="Test Customer", customer_email="cust@example.com",
    customer_phone="+910000000000",
    cart_value=500.0, final_amount_charged=500.0,
    cart_json=json.dumps([]),
    status=SessionStatus.ABANDONED,
    recovered_from_session_id=None,
)
db.add(sess_idem)
case_idem = RecoveryCase(
    id=904, merchant_id=1, customer_user_id=1,
    checkout_session_id=805,
    scenario=RecoveryScenario.PAYMENT_FAILURE,
    amount_at_risk=500.0, amount_recovered=0.0,
    status=CaseStatus.INTERVENING,
    ladder_step=1, classification="retryable_technical",
    classification_source=ClassificationMethod.RULE,
    error_source="gateway", rar_score=60.0, confidence=1.0,
    escalated_to_human=False, contact_touches=1,
    last_action_at=datetime.now(timezone.utc),
)
db.add(case_idem)
db.commit()
db.close()

res1 = call_complete("evt_rc_idem")
check("first call HTTP 200", res1.status_code == 200, res1.status_code)
res2 = call_complete("evt_rc_idem")   # second call — session already COMPLETED
# Should succeed (or 200 is acceptable); must NOT create a second audit log row
check("second call does not 500", res2.status_code != 500, res2.status_code)

db = TestingSessionLocal()
dup_logs = db.query(RecoveryActionLog).filter(
    RecoveryActionLog.case_id == 904,
    RecoveryActionLog.action_type == "case_recovered",
).all()
check("still exactly 1 audit log after double-call", len(dup_logs) == 1, len(dup_logs))
db.close()

# ── [4] Already-RECOVERED case is untouched ───────────────────────────────────
print("\n[4] Already-RECOVERED guard — pre-RECOVERED case must not be modified")
res = call_complete("evt_rc_already")
check("HTTP 200", res.status_code == 200, res.status_code)

db = TestingSessionLocal()
case = db.query(RecoveryCase).filter(RecoveryCase.id == 903).first()
check("case.status still RECOVERED", case.status == CaseStatus.RECOVERED, case.status)
check("case.amount_recovered still 1000.0", case.amount_recovered == 1000.0, case.amount_recovered)
logs = db.query(RecoveryActionLog).filter(
    RecoveryActionLog.case_id == 903,
    RecoveryActionLog.action_type == "case_recovered",
).all()
check("no new audit log written for already-RECOVERED case", len(logs) == 0, len(logs))
db.close()

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'=' * 70}")
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  ❌ {f}")
    sys.exit(1)
else:
    print("RESULT: ALL RECOVERED-TRANSITION TESTS PASSED ✅")
print("=" * 70)
