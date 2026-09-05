"""
Test Suite: Overdue Receivables Voice Call Capability
=====================================================
Verifies:
  1. Manual voice calling from /merchant/priority for escalated overdue invoice cases.
  2. Proper logging of action_type="manual_voice_call" in RecoveryActionLog.
  3. Confirmation that reconcile_overdue_invoices() does NOT fire voice calls autonomously.
"""

import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest
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


@pytest.fixture
def test_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSession()

    merchant = MerchantUser(
        id=1, email="merchant@example.com", password_hash="hash",
        store_name="Receivables Store", max_discount_pct=15
    )
    customer = CustomerUser(
        id=1, email="client@example.com", name="Acme Corp Client",
        phone="+919880640064", password_hash="hash"
    )
    db.add_all([merchant, customer])
    db.commit()

    # Create an overdue invoice
    now = datetime.now(timezone.utc)
    inv = Invoice(
        id=10,
        merchant_id=1,
        customer_user_id=1,
        invoice_number="INV-2026-TEST-99",
        amount=8500.0,
        currency="INR",
        issue_date=now - timedelta(days=35),
        due_date=now - timedelta(days=32),  # 32 days overdue
        status=InvoiceStatus.OVERDUE,
        payment_link_url="https://pay.razorpay.com/inv_test_99"
    )
    db.add(inv)
    db.commit()

    # Create an escalated RecoveryCase for this invoice
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
    db.add(case)
    db.commit()

    yield db
    db.close()


def test_receivables_manual_voice_call(test_db):
    """Test manual voice call trigger on escalated overdue invoice case."""
    def override_db():
        yield test_db

    def override_merchant(db: Session = Depends(get_db)):
        return test_db.query(MerchantUser).filter(MerchantUser.id == 1).first()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_merchant] = override_merchant

    token = create_merchant_session_token(1)
    client = TestClient(app, cookies={MERCHANT_COOKIE_NAME: token})

    with patch("app.voice_service.make_invoice_overdue_call") as mock_call, \
         patch("app.sms_service.send_recovery_sms") as mock_sms:
        
        mock_call.return_value = {"status": "initiated", "sid": "CA_test_call_sid_123"}
        mock_sms.return_value = {"status": "sent", "sid": "SM_test_sms_sid_123"}

        res = client.post("/api/merchant/priority/case_50/call")
        assert res.status_code == 200, f"Expected 200, got {res.status_code}: {res.text}"
        data = res.json()
        assert data["status"] == "success"
        assert data["call_sid"] == "CA_test_call_sid_123"

        # Verify voice call function was invoked with exact invoice context
        mock_call.assert_called_once()
        args, kwargs = mock_call.call_args
        # args: to_phone, customer_name, invoice_num, inv_amount, days_overdue, pay_url
        assert args[0] == "+919880640064"
        assert args[1] == "Acme Corp Client"
        assert args[2] == "INV-2026-TEST-99"
        assert args[3] == 8500.0
        assert args[4] >= 31  # ~32 days overdue

        # Verify RecoveryActionLog record
        action_log = (
            test_db.query(RecoveryActionLog)
            .filter(RecoveryActionLog.case_id == 50)
            .order_by(RecoveryActionLog.id.desc())
            .first()
        )
        assert action_log is not None
        assert action_log.action_type == "manual_voice_call"
        assert action_log.requires_human_approval is False
        assert action_log.outcome == "sent"
        assert "INV-2026-TEST-99" in action_log.reason
        assert "32 days overdue" in action_log.reason or "days overdue" in action_log.reason

        # Verify case contact touches incremented
        case = test_db.query(RecoveryCase).filter(RecoveryCase.id == 50).first()
        assert case.contact_touches == 3

    app.dependency_overrides.clear()


def test_reconcile_overdue_invoices_never_calls_voice(test_db):
    """Verify autonomous reconcile_overdue_invoices() ladder never fires voice calls."""
    # Add a new step-1 overdue invoice
    now = datetime.now(timezone.utc)
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
    test_db.add_all([inv2, case2])
    test_db.commit()

    with patch("app.voice_service.make_invoice_overdue_call") as mock_inv_call, \
         patch("app.voice_service.make_recovery_call") as mock_rec_call, \
         patch("app.agent.receivables_dunning.send_recovery_email") as mock_email:
        
        mock_email.return_value = "sent"

        # Run automated ladder
        reconcile_overdue_invoices(test_db)

        # Autonomous ladder should send email reminder ONLY
        assert mock_email.called
        assert not mock_inv_call.called, "reconcile_overdue_invoices() must NOT fire voice calls autonomously!"
        assert not mock_rec_call.called, "reconcile_overdue_invoices() must NOT fire voice calls autonomously!"
