"""
Integration smoke-test: payment failure action ladder.

Tests:
  1. risk_terminal  -> ESCALATED, human_escalation logged, no comms
  2. low_confidence -> ESCALATED, low_confidence reason
  3. needs_customer_action -> INTERVENING, ux_suppressed logged
  4. needs_alternate_method -> INTERVENING, payment_link_sent, contact_touches incremented
  5. retryable_technical   -> INTERVENING, payment_link_sent
  6. circuit breaker (contact_touches >= 3) -> LOST
  7. abandonment pipeline unchanged (no payment_status_code)
  8. discount guard invariant

Run from project root:
    .venv_new\Scripts\python.exe scripts\test_payment_failure_actions.py
"""

import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MOCK_PAYMENTS", "true")
os.environ.setdefault("RECOVERY_MODE", "simulated")

# Use an in-memory SQLite DB for the test — doesn't touch storefront.db
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.db_models import (
    Base, CheckoutSession, RecoveryCase, RecoveryActionLog, CaseStatus,
    CustomerUser, SessionStatus,
)

engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
Base.metadata.create_all(bind=engine)
TestSession = sessionmaker(bind=engine)

# Seed a single reusable customer row (customer_user_id FK on CheckoutSession is not nullable)
_setup_db = TestSession()
_dummy_customer = CustomerUser(email="test@example.com", name="Test", phone="+919999999999", password_hash="x")
_setup_db.add(_dummy_customer)
_setup_db.commit()
DUMMY_CUSTOMER_ID = _dummy_customer.id
_setup_db.close()

from app.agent.payment_failure_classifier import PaymentFailureClassification
from app.agent.payment_failure_actions import run_payment_failure_recovery
from app.razorpay_client import SimulatedRazorpayClient
from datetime import datetime, timezone

SIM_CLIENT = SimulatedRazorpayClient(paid_rate=0.0, failure_rate=0.0)  # always creates link, never pays


def make_session(db, event_id="evt_001", cart_value=1500.0, payment_status_code="insufficient_funds"):
    """Creates a minimal CheckoutSession row in the test DB."""
    s = CheckoutSession(
        event_id=event_id,
        customer_email="test@example.com",
        customer_phone="+919999999999",
        customer_name="Test Customer",
        cart_value=cart_value,
        cart_json=json.dumps([]),           # empty cart fine for action tests
        payment_status_code=payment_status_code,
        customer_user_id=DUMMY_CUSTOMER_ID,
        status=SessionStatus.ABANDONED,
    )
    db.add(s)
    db.flush()
    return s


def make_classification(
    session_id,
    failure_class="needs_alternate_method",
    source="bank",
    confidence=1.0,
    method="rule",
    status_code="insufficient_funds",
):
    return PaymentFailureClassification(
        session_id=session_id,
        status_code=status_code,
        source=source,
        failure_class=failure_class,
        description=f"Test: {failure_class}",
        confidence=confidence,
        method=method,
    )


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
print("Payment Failure Action Ladder — integration smoke-test")
print("=" * 70)

# ────────────────────────────────────────────────────────────────────────────
print("\n[1] risk_terminal → ESCALATED, human_escalation logged")
db = TestSession()
sess = make_session(db, "evt_rt_01", payment_status_code="risk_blocked")
clf = make_classification(sess.event_id, failure_class="risk_terminal", source="business", status_code="risk_blocked")
case = run_payment_failure_recovery(sess, clf, db, SIM_CLIENT)
log = db.query(RecoveryActionLog).filter(RecoveryActionLog.case_id == case.id).first()
check("status=ESCALATED", case.status == CaseStatus.ESCALATED, case.status)
check("escalated_to_human=True", case.escalated_to_human is True)
check("escalation_reason=risk_block", case.escalation_reason == "risk_block", case.escalation_reason)
check("action_type=human_escalation", log and log.action_type == "human_escalation", log and log.action_type)
check("outcome=sent", log and log.outcome == "sent", log and log.outcome)
check("contact_touches=0 (no comms)", case.contact_touches == 0, case.contact_touches)
db.close()

# ────────────────────────────────────────────────────────────────────────────
print("\n[2] low_confidence → ESCALATED, low_confidence reason")
db = TestSession()
sess = make_session(db, "evt_lc_01", payment_status_code="some_weird_code")
clf = make_classification(sess.event_id, failure_class="unknown", confidence=0.2, method="llm", status_code="some_weird_code")
case = run_payment_failure_recovery(sess, clf, db, SIM_CLIENT)
log = db.query(RecoveryActionLog).filter(RecoveryActionLog.case_id == case.id).first()
check("status=ESCALATED", case.status == CaseStatus.ESCALATED, case.status)
check("escalation_reason=low_confidence", case.escalation_reason == "low_confidence", case.escalation_reason)
check("contact_touches=0", case.contact_touches == 0)
db.close()

# ────────────────────────────────────────────────────────────────────────────
print("\n[3] needs_customer_action → INTERVENING, ux_suppressed, no link")
db = TestSession()
sess = make_session(db, "evt_ca_01", payment_status_code="otp_invalid")
clf = make_classification(sess.event_id, failure_class="needs_customer_action", source="customer", status_code="otp_invalid")
case = run_payment_failure_recovery(sess, clf, db, SIM_CLIENT)
log = db.query(RecoveryActionLog).filter(RecoveryActionLog.case_id == case.id).first()
check("status=INTERVENING", case.status == CaseStatus.INTERVENING, case.status)
check("action_type=ux_suppressed", log and log.action_type == "ux_suppressed", log and log.action_type)
check("outcome=suppressed", log and log.outcome == "suppressed", log and log.outcome)
check("contact_touches=0 (no comms sent)", case.contact_touches == 0, case.contact_touches)
db.close()

# ────────────────────────────────────────────────────────────────────────────
print("\n[4] needs_alternate_method → INTERVENING, payment_link_sent, touches++")
db = TestSession()
sess = make_session(db, "evt_am_01", cart_value=2999.0, payment_status_code="card_expired")
clf = make_classification(sess.event_id, failure_class="needs_alternate_method", source="bank", status_code="card_expired")
case = run_payment_failure_recovery(sess, clf, db, SIM_CLIENT)
logs = db.query(RecoveryActionLog).filter(RecoveryActionLog.case_id == case.id).all()
log = logs[0] if logs else None
check("status=INTERVENING", case.status == CaseStatus.INTERVENING, case.status)
check("action_type=payment_link_sent", log and log.action_type == "payment_link_sent", log and log.action_type)
check("outcome=sent", log and log.outcome == "sent", log and log.outcome)
check("amount_offered=cart_value (no discount)", log and log.amount_offered == 2999.0, log and log.amount_offered)
check("coupon_code=None (never for PF)", log and log.coupon_code is None, log and log.coupon_code)
check("contact_touches=1", case.contact_touches == 1, case.contact_touches)
db.close()

# ────────────────────────────────────────────────────────────────────────────
print("\n[5] retryable_technical → INTERVENING, payment_link_sent")
db = TestSession()
sess = make_session(db, "evt_rt_02", payment_status_code="gateway_timeout")
clf = make_classification(sess.event_id, failure_class="retryable_technical", source="gateway", status_code="gateway_timeout")
case = run_payment_failure_recovery(sess, clf, db, SIM_CLIENT)
log = db.query(RecoveryActionLog).filter(RecoveryActionLog.case_id == case.id).first()
check("status=INTERVENING", case.status == CaseStatus.INTERVENING, case.status)
check("action_type=payment_link_sent", log and log.action_type == "payment_link_sent", log and log.action_type)
check("contact_touches=1", case.contact_touches == 1, case.contact_touches)
db.close()

# ────────────────────────────────────────────────────────────────────────────
print("\n[6] circuit breaker: contact_touches >= 3 → LOST")
db = TestSession()
sess = make_session(db, "evt_cb_01", payment_status_code="issuer_decline")
clf = make_classification(sess.event_id, failure_class="needs_alternate_method", source="bank", status_code="issuer_decline")
# Run 3 times to exhaust touches
for i in range(3):
    run_payment_failure_recovery(sess, clf, db, SIM_CLIENT)
# 4th run should trip the circuit breaker
case = run_payment_failure_recovery(sess, clf, db, SIM_CLIENT)
check("status=LOST after 3 touches", case.status == CaseStatus.LOST, case.status)
check("contact_touches=3 (not incremented past limit)", case.contact_touches == 3, case.contact_touches)
db.close()

# ────────────────────────────────────────────────────────────────────────────
print("\n[7] idempotency: re-running on ESCALATED case returns same state")
db = TestSession()
sess = make_session(db, "evt_idem_01", payment_status_code="risk_blocked")
clf = make_classification(sess.event_id, failure_class="risk_terminal", source="business", status_code="risk_blocked")
case1 = run_payment_failure_recovery(sess, clf, db, SIM_CLIENT)
case2 = run_payment_failure_recovery(sess, clf, db, SIM_CLIENT)  # second call
log_count = db.query(RecoveryActionLog).filter(RecoveryActionLog.case_id == case1.id).count()
check("same case returned", case1.id == case2.id, f"case1={case1.id} case2={case2.id}")
check("terminal state preserved", case2.status == CaseStatus.ESCALATED, case2.status)
check("only 1 action log (not duplicated)", log_count == 1, f"log_count={log_count}")
db.close()

# ────────────────────────────────────────────────────────────────────────────
print("\n[8] abandonment pipeline import smoke-test (no changes to run_recovery_for_session)")
try:
    from app.live_recovery import run_recovery_for_session
    from app.agent.classifier import classify, rule_based_classify
    from app.agent.recovery_actions import decide_action, execute_action, is_discount_allowed
    from app.guardrails import check_guardrails, cap_discount
    print(f"  {PASS}  All abandonment pipeline modules import cleanly")
    check("is_discount_allowed unchanged for card_declined", not is_discount_allowed("card_declined"))
    check("is_discount_allowed unchanged for high_amount_hesitation", is_discount_allowed("high_amount_hesitation"))
    check("cap_discount unchanged at 15%", cap_discount(50) == 15)
except ImportError as e:
    check("abandonment modules import", False, str(e))

# ────────────────────────────────────────────────────────────────────────────
print(f"\n{'='*70}")
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  ❌ {f}")
    sys.exit(1)
else:
    print(f"RESULT: ALL 8 SCENARIOS PASSED ✅")
print("=" * 70)
