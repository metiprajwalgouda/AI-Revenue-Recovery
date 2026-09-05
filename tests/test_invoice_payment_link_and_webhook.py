"""
End-to-end tests for Invoice Razorpay Payment Links, Reminder Email Link rendering,
Customer Confirmation Callback, and Real Razorpay Webhook Processing.
"""

import os
import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db_models import (
    Base, MerchantUser, CustomerUser,
    Invoice, InvoiceStatus,
    RecoveryCase, RecoveryActionLog,
    RecoveryScenario, CaseStatus,
)
from app.db import get_db
from app.main import app
from app.merchant_auth_routes import get_current_merchant
from app.razorpay_client import get_razorpay_client, SimulatedRazorpayClient, PaymentLinkResult
from app.agent.receivables_dunning import reconcile_overdue_invoices


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)


@pytest.fixture()
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def seed_merchant_customer(db_session):
    merchant = MerchantUser(
        email="test_invoice_merchant@example.com",
        password_hash="hashed_pw",
        store_name="Acme Billing",
    )
    customer = CustomerUser(
        email="client@example.com",
        password_hash="hashed_pw",
        name="Alice Client",
        phone="+919876543210",
        opted_out_of_marketing=False,
    )
    db_session.add_all([merchant, customer])
    db_session.commit()
    db_session.refresh(merchant)
    db_session.refresh(customer)
    return merchant, customer


def test_issue_invoice_creates_real_razorpay_payment_link(client, seed_merchant_customer, db_session):
    """Test 1: POST /api/merchant/invoices creates a real Razorpay payment link."""
    merchant, customer = seed_merchant_customer
    app.dependency_overrides[get_current_merchant] = lambda: merchant

    # Mock razorpay client to return a known payment link
    mock_rzp = MagicMock()
    mock_rzp.create_recovery_payment_link.return_value = PaymentLinkResult(
        success=True,
        payment_link_id="plink_TEST123456",
        short_url="https://rzp.io/i/testlink123",
    )
    app.dependency_overrides[get_razorpay_client] = lambda: mock_rzp

    res = client.post("/api/merchant/invoices", json={
        "customer_id": customer.id,
        "amount": 7500.0,
        "due_date": "5m",
        "invoice_number": "INV-TEST-PLINK-01",
    })

    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "success"
    assert data["invoice"]["payment_link_url"] == "https://rzp.io/i/testlink123"
    assert data["invoice"]["razorpay_invoice_id"] == "plink_TEST123456"

    # Verify stored in DB
    inv = db_session.query(Invoice).filter(Invoice.invoice_number == "INV-TEST-PLINK-01").first()
    assert inv is not None
    assert inv.payment_link_url == "https://rzp.io/i/testlink123"
    assert inv.razorpay_invoice_id == "plink_TEST123456"

    case = db_session.query(RecoveryCase).filter(RecoveryCase.invoice_id == inv.id).first()
    assert case is not None
    assert case.invoice.payment_link_url == "https://rzp.io/i/testlink123"
    assert case.invoice.razorpay_invoice_id == "plink_TEST123456"


@patch("app.agent.receivables_dunning.send_recovery_email", return_value=True)
def test_reminder_email_renders_real_payment_link_url(mock_email, client, seed_merchant_customer, db_session):
    """Test 2: Dunning reminder email contains the real payment_link_url in context."""
    merchant, customer = seed_merchant_customer

    due_past = datetime.now(timezone.utc) - timedelta(minutes=5)
    inv = Invoice(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        invoice_number="INV-TEST-EMAIL-01",
        amount=4200.0,
        currency="INR",
        issue_date=due_past - timedelta(days=5),
        due_date=due_past,
        status=InvoiceStatus.PENDING,
        payment_link_url="https://rzp.io/i/real_pay_link_99",
        razorpay_invoice_id="plink_real_99",
    )
    db_session.add(inv)
    db_session.flush()

    case = RecoveryCase(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        invoice_id=inv.id,
        scenario=RecoveryScenario.OVERDUE_RECEIVABLE,
        amount_at_risk=4200.0,
        amount_recovered=0.0,
        status=CaseStatus.NEW,
        ladder_step=0,
    )
    db_session.add(case)
    db_session.commit()

    # Run dunning poller
    result = reconcile_overdue_invoices(db_session)
    assert result["step1_sent"] == 1

    # Verify email was dispatched with the real payment_link_url in template context
    mock_email.assert_called_once()
    args, kwargs = mock_email.call_args
    context = kwargs.get("context") or args[3]
    assert context["payment_link_url"] == "https://rzp.io/i/real_pay_link_99"
    assert context["amount"] == 4200.0
    assert context["invoice_number"] == "INV-TEST-EMAIL-01"


def test_razorpay_webhook_payment_link_paid(client, seed_merchant_customer, db_session):
    """Test 3: POST /api/webhooks/razorpay with payment_link.paid event closes case as RECOVERED."""
    merchant, customer = seed_merchant_customer

    inv = Invoice(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        invoice_number="INV-WEBHOOK-001",
        amount=9000.0,
        currency="INR",
        issue_date=datetime.now(timezone.utc) - timedelta(days=10),
        due_date=datetime.now(timezone.utc) - timedelta(days=2),
        status=InvoiceStatus.OVERDUE,
        payment_link_url="https://rzp.io/i/webhook_link_01",
        razorpay_invoice_id="plink_webhook_001",
    )
    db_session.add(inv)
    db_session.flush()

    case = RecoveryCase(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        invoice_id=inv.id,
        scenario=RecoveryScenario.OVERDUE_RECEIVABLE,
        amount_at_risk=9000.0,
        amount_recovered=0.0,
        status=CaseStatus.INTERVENING,
        ladder_step=1,
        next_action_due_at=datetime.now(timezone.utc) + timedelta(days=5),
    )
    db_session.add(case)
    db_session.commit()

    webhook_payload = {
        "entity": "event",
        "account_id": "acc_test123",
        "event": "payment_link.paid",
        "contains": ["payment_link", "payment"],
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_webhook_001",
                    "amount": 900000,
                    "amount_paid": 900000,
                    "currency": "INR",
                    "status": "paid",
                    "reference_id": "inv_INV-WEBHOOK-001_abc",
                    "notes": {
                        "invoice_id": str(inv.id),
                        "invoice_number": inv.invoice_number,
                    }
                }
            },
            "payment": {
                "entity": {
                    "id": "pay_WH_99999",
                    "amount": 900000,
                    "currency": "INR",
                    "status": "captured",
                }
            }
        }
    }

    with patch("app.razorpay_client.RazorpayRecoveryClient.verify_webhook_signature", return_value=True), \
         patch("app.razorpay_client.SimulatedRazorpayClient.verify_webhook_signature", return_value=True):
        res = client.post(
            "/api/webhooks/razorpay",
            json=webhook_payload,
            headers={"X-Razorpay-Signature": "valid_signature"},
        )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "success"
    assert data["invoice_status"] == "paid"
    assert data["case_status"] == "recovered"

    # Verify DB updates
    db_session.refresh(inv)
    db_session.refresh(case)

    assert inv.status == InvoiceStatus.PAID
    assert case.status == CaseStatus.RECOVERED
    assert case.amount_recovered == 9000.0
    assert case.next_action_due_at is None

    # Verify audit log row
    log = db_session.query(RecoveryActionLog).filter(
        RecoveryActionLog.case_id == case.id,
        RecoveryActionLog.action_type == "case_recovered",
    ).first()
    assert log is not None
    assert log.outcome == "sent"
    guardrails = json.loads(log.guardrail_checks)
    assert guardrails["confirmed_payment_id"] == "pay_WH_99999"
    assert guardrails["channel"] == "razorpay_webhook"


def test_razorpay_webhook_invalid_signature_rejected(client, seed_merchant_customer, db_session):
    """Test 4: Invalid signature on webhook is rejected with HTTP 400."""
    webhook_payload = {
        "event": "payment_link.paid",
        "payload": {"payment_link": {"entity": {"id": "plink_xyz"}}}
    }

    res = client.post(
        "/api/webhooks/razorpay",
        json=webhook_payload,
        headers={"X-Razorpay-Signature": "invalid_signature"},
    )
    assert res.status_code == 400
    assert "Invalid webhook signature" in res.json().get("detail", "")


def test_customer_confirmation_page_marks_invoice_paid(client, seed_merchant_customer, db_session):
    """Test 5: GET /invoice/{number}/confirmation redirects and marks invoice PAID."""
    merchant, customer = seed_merchant_customer

    inv = Invoice(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        invoice_number="INV-CONFIRM-01",
        amount=1500.0,
        currency="INR",
        issue_date=datetime.now(timezone.utc) - timedelta(days=2),
        due_date=datetime.now(timezone.utc) + timedelta(days=5),
        status=InvoiceStatus.PENDING,
        payment_link_url="https://rzp.io/i/confirm_link",
        razorpay_invoice_id="plink_confirm_01",
    )
    db_session.add(inv)
    db_session.flush()

    case = RecoveryCase(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        invoice_id=inv.id,
        scenario=RecoveryScenario.OVERDUE_RECEIVABLE,
        amount_at_risk=1500.0,
        amount_recovered=0.0,
        status=CaseStatus.NEW,
        ladder_step=0,
    )
    db_session.add(case)
    db_session.commit()

    # Customer visits redirect callback URL after paying in Razorpay
    res = client.get(
        f"/invoice/{inv.invoice_number}/confirmation",
        params={
            "razorpay_payment_id": "pay_CALLBACK_123",
            "razorpay_payment_link_id": "plink_confirm_01",
            "razorpay_payment_link_status": "paid",
        },
    )
    assert res.status_code == 200
    assert "Payment Successful!" in res.text
    assert inv.invoice_number in res.text

    # Verify DB state
    db_session.refresh(inv)
    db_session.refresh(case)
    assert inv.status == InvoiceStatus.PAID
    assert case.status == CaseStatus.RECOVERED
    assert case.amount_recovered == 1500.0
    assert case.next_action_due_at is None


def test_poller_fallback_reconciles_paid_payment_link(seed_merchant_customer, db_session):
    """Test 6: Overdue/Pending invoice paid on Razorpay is reconciled to PAID/RECOVERED via poller fallback without webhook/redirect."""
    merchant, customer = seed_merchant_customer

    inv = Invoice(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        invoice_number="INV-POLLER-TEST-01",
        amount=5500.0,
        currency="INR",
        issue_date=datetime.now(timezone.utc) - timedelta(days=3),
        due_date=datetime.now(timezone.utc) - timedelta(days=1),
        status=InvoiceStatus.OVERDUE,
        payment_link_url="https://rzp.io/i/poller_link_01",
        razorpay_invoice_id="plink_POLLER_12345",
    )
    db_session.add(inv)
    db_session.flush()

    case = RecoveryCase(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        invoice_id=inv.id,
        scenario=RecoveryScenario.OVERDUE_RECEIVABLE,
        amount_at_risk=5500.0,
        amount_recovered=0.0,
        status=CaseStatus.INTERVENING,
        ladder_step=1,
        next_action_due_at=datetime.now(timezone.utc) + timedelta(days=6),
    )
    db_session.add(case)
    db_session.commit()

    # Mock razorpay client status check returning paid string
    mock_rzp = MagicMock()
    mock_rzp.fetch_payment_link_status.return_value = "paid"

    with patch("app.agent.receivables_dunning.get_razorpay_client", return_value=mock_rzp):
        result = reconcile_overdue_invoices(db_session)

    assert result["poller_reconciled_paid"] == 1

    # Verify DB updates
    db_session.refresh(inv)
    db_session.refresh(case)

    assert inv.status == InvoiceStatus.PAID
    assert case.status == CaseStatus.RECOVERED
    assert case.amount_recovered == 5500.0
    assert case.next_action_due_at is None

    # Verify audit log row
    log = db_session.query(RecoveryActionLog).filter(
        RecoveryActionLog.case_id == case.id,
        RecoveryActionLog.action_type == "case_recovered",
    ).first()
    assert log is not None
    assert log.outcome == "sent"
    guardrails = json.loads(log.guardrail_checks)
    assert guardrails["confirmed_payment_id"] == f"poll_{inv.razorpay_invoice_id}"
    assert guardrails["channel"] == "poller_reconciliation"


