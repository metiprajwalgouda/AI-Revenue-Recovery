"""
Standalone Verification Script: Overdue Receivables Voice Call
==============================================================
Tests:
  1. Priority queue manual call endpoint on overdue invoice case.
  2. RecoveryActionLog audit entry generated with action_type='manual_voice_call'.
  3. Reconcile overdue invoices automated ladder never fires voice calls.
"""

import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from fastapi import Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.db_models import (
    Base, MerchantUser, CustomerUser, Invoice, InvoiceStatus,
    RecoveryCase, RecoveryActionLog, RecoveryScenario, CaseStatus
)
from app.merchant_auth_routes import get_current_merchant
from app.auth import create_merchant_session_token, MERCHANT_COOKIE_NAME
from app.main import app
from app.agent.receivables_dunning import reconcile_overdue_invoices

engine = create_engine(
    "sqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool
)
Base.metadata.create_all(bind=engine)
TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
db = TestingSession()

# Setup fixtures
merchant = MerchantUser(
    id=1, email="merchant@example.com", password_hash="hash",
    store_name="Receivables Store", max_discount_pct=15
)
customer = CustomerUser(
    id=1, email="client@example.com", name="Acme Corp Client",
    phone="+919880640064", password_hash="hash"
)
db.add_all([merchant, customer])

now = datetime.now(timezone.utc)
inv = Invoice(
    id=10,
    merchant_id=1,
    customer_user_id=1,
    invoice_number="INV-2026-TEST-99",
    amount=8500.0,
    currency="INR",
    issue_date=now - timedelta(days=35),
    due_date=now - timedelta(days=32),
    status=InvoiceStatus.OVERDUE,
    payment_link_url="https://pay.razorpay.com/inv_test_99"
)
case = RecoveryCase(
    id=50,
    merchant_id=1,
    customer_user_id=1,
    invoice_id=10,
    scenario=RecoveryScenario.OVERDUE_RECEIVABLE,
    amount_at_risk=8500.0,
    amount_recovered=0.0,
    status=CaseStatus.ESCALATED,
    escalated_to_human=True,
    escalation_reason="receivable_30d_overdue",
    ladder_step=3,
    contact_touches=2,
)
db.add_all([inv, case])
db.commit()

def override_db():
    yield db

def override_merchant(d: Session = Depends(get_db)):
    return db.query(MerchantUser).filter(MerchantUser.id == 1).first()

app.dependency_overrides[get_db] = override_db
app.dependency_overrides[get_current_merchant] = override_merchant

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

print("=" * 75)
print("Overdue Receivables Manual Voice Call Verification")
print("=" * 75)

# Test 1: Manual Call
print("\n[1] Testing POST /api/merchant/priority/case_50/call")
with patch("app.voice_service.make_invoice_overdue_call") as mock_call, \
     patch("app.sms_service.send_recovery_sms") as mock_sms:
    
    mock_call.return_value = {"status": "initiated", "sid": "CA_test_call_sid_123"}
    mock_sms.return_value = {"status": "sent", "sid": "SM_test_sms_sid_123"}

    res = client.post("/api/merchant/priority/case_50/call")
    check("Endpoint returned 200", res.status_code == 200, res.text)
    data = res.json()
    check("Response status is success", data.get("status") == "success", data)
    check("Call SID matches", data.get("call_sid") == "CA_test_call_sid_123", data)

    # Check mock call arguments
    mock_call.assert_called_once()
    args, kwargs = mock_call.call_args
    check("Phone number passed correctly", args[0] == "+919880640064", args[0])
    check("Customer name passed correctly", args[1] == "Acme Corp Client", args[1])
    check("Invoice number passed correctly", args[2] == "INV-2026-TEST-99", args[2])
    check("Amount passed correctly", args[3] == 8500.0, args[3])
    check("Days overdue calculated correctly", args[4] >= 31, args[4])

    # Check audit log
    action_log = (
        db.query(RecoveryActionLog)
        .filter(RecoveryActionLog.case_id == 50)
        .order_by(RecoveryActionLog.id.desc())
        .first()
    )
    check("RecoveryActionLog created", action_log is not None)
    if action_log:
        check("action_type is manual_voice_call", action_log.action_type == "manual_voice_call", action_log.action_type)
        check("outcome is sent", action_log.outcome == "sent", action_log.outcome)
        check("requires_human_approval is False", action_log.requires_human_approval is False)
        check("Reason contains invoice number", "INV-2026-TEST-99" in (action_log.reason or ""))

# Test 2: Verify automated ladder never calls voice
print("\n[2] Testing reconcile_overdue_invoices() Autonomous Invariant")
inv2 = Invoice(
    id=11,
    merchant_id=1,
    customer_user_id=1,
    invoice_number="INV-2026-AUTO-01",
    amount=3000.0,
    currency="INR",
    issue_date=now - timedelta(days=5),
    due_date=now - timedelta(days=1),
    status=InvoiceStatus.PENDING,
)
case2 = RecoveryCase(
    id=51,
    merchant_id=1,
    customer_user_id=1,
    invoice_id=11,
    scenario=RecoveryScenario.OVERDUE_RECEIVABLE,
    amount_at_risk=3000.0,
    amount_recovered=0.0,
    status=CaseStatus.NEW,
    ladder_step=0,
    contact_touches=0,
)
db.add_all([inv2, case2])
db.commit()

with patch("app.voice_service.make_invoice_overdue_call") as mock_inv_call, \
     patch("app.voice_service.make_recovery_call") as mock_rec_call, \
     patch("app.agent.receivables_dunning.send_recovery_email") as mock_email:
    
    mock_email.return_value = "sent"
    reconcile_overdue_invoices(db)

    check("Automated email was sent", mock_email.called)
    check("make_invoice_overdue_call was NOT called", not mock_inv_call.called)
    check("make_recovery_call was NOT called", not mock_rec_call.called)

print("\n" + "=" * 75)
if failures:
    print(f"FAILED: {len(failures)} checks failed")
    sys.exit(1)
else:
    print("ALL OVERDUE RECEIVABLES VOICE CALL TESTS PASSED ✅")
    sys.exit(0)
