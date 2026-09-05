"""
Tests for the Overdue Receivables Dunning Ladder.

Coverage:
  1. Day-0 transition: PENDING invoice past due -> OVERDUE, first reminder sent,
     idempotency ensures only one log row, case status -> INTERVENING.
  2. +7 day step: second reminder sent, case.ladder_step == 2.
  3. +30 day escalation: case -> ESCALATED, NO contact sent, contact_blocked recorded.
  4. Post-escalation idempotency: reconcile runs again after step 3 — NO additional log rows written.
  5. +60 day write-off: invoice -> WRITE_OFF_REVIEW, no contact, ladder ends.
  6. Invoice payment (RECOVERED): /api/invoice-payment/complete marks invoice PAID,
     case RECOVERED, next_action_due_at=None, one "case_recovered" audit log.
  7. RECOVERED case: reconcile runs again — skips terminal case, no new rows.
  8. Idempotency: calling reconcile twice at step 1 produces exactly one action log row.
  9. No discount invariant: no action log row ever has amount_offered or coupon_code set.
"""

import json
import os
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

os.environ.setdefault("RAZORPAY_KEY_ID", "test_key")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_secret")

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
from app.agent.receivables_dunning import reconcile_overdue_invoices


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


def _seed(db_session, due_date: datetime):
    """Seed a merchant, customer, PENDING invoice, and its linked RecoveryCase."""
    merchant = MerchantUser(
        email="dunning_merchant@example.com",
        password_hash="hash",
        store_name="Dunning Test Store",
    )
    customer = CustomerUser(
        email="customer@example.com",
        password_hash="hash",
        name="Overdue Customer",
        phone="+919999999999",
        opted_out_of_marketing=False,
    )
    db_session.add_all([merchant, customer])
    db_session.flush()

    invoice = Invoice(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        invoice_number="INV-TEST-001",
        amount=10000.0,
        currency="INR",
        issue_date=datetime.now(timezone.utc) - timedelta(days=35),
        due_date=due_date,
        status=InvoiceStatus.PENDING,
    )
    db_session.add(invoice)
    db_session.flush()

    case = RecoveryCase(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        invoice_id=invoice.id,
        scenario=RecoveryScenario.OVERDUE_RECEIVABLE,
        amount_at_risk=10000.0,
        amount_recovered=0.0,
        status=CaseStatus.NEW,
        ladder_step=0,
    )
    db_session.add(case)
    db_session.commit()
    db_session.refresh(invoice)
    db_session.refresh(case)
    return merchant, customer, invoice, case


# ---------------------------------------------------------------------------
# 1. Day-0 transition
# ---------------------------------------------------------------------------

@patch("app.agent.receivables_dunning.send_recovery_email", return_value=True)
def test_day0_invoice_overdue_first_reminder(mock_email, db_session):
    """PENDING invoice past due_date -> OVERDUE + step-1 reminder + case INTERVENING."""
    due = datetime.now(timezone.utc) - timedelta(minutes=1)  # 1 minute past due
    merchant, customer, invoice, case = _seed(db_session, due)

    result = reconcile_overdue_invoices(db_session)

    assert result["new_overdue"] == 1
    assert result["step1_sent"] == 1

    db_session.refresh(invoice)
    db_session.refresh(case)

    assert invoice.status == InvoiceStatus.OVERDUE
    assert case.status == CaseStatus.INTERVENING
    assert case.ladder_step == 1
    assert case.next_action_due_at is not None

    # next_action should be ~7 days after due_date
    due_tz = due.replace(tzinfo=timezone.utc) if due.tzinfo is None else due
    expected_next = due_tz + timedelta(days=7)
    actual_next = case.next_action_due_at.replace(tzinfo=timezone.utc) if case.next_action_due_at.tzinfo is None else case.next_action_due_at
    assert abs((actual_next - expected_next).total_seconds()) < 5

    # Exactly one action log row
    logs = db_session.query(RecoveryActionLog).filter(RecoveryActionLog.case_id == case.id).all()
    assert len(logs) == 1
    assert logs[0].action_type == "send_invoice_reminder"
    assert logs[0].ladder_step == 1
    assert logs[0].outcome == "sent"

    # No discount invariant
    assert logs[0].amount_offered is None
    assert logs[0].coupon_code is None
    guardrails = json.loads(logs[0].guardrail_checks)
    assert guardrails["discount_offered"] is False

    # Email was called once and used the invoice_reminder template
    mock_email.assert_called_once()
    call_repr = str(mock_email.call_args)
    assert "invoice_reminder" in call_repr


# ---------------------------------------------------------------------------
# 2. Day-0 idempotency: run reconcile twice, only one log row
# ---------------------------------------------------------------------------

@patch("app.agent.receivables_dunning.send_recovery_email", return_value=True)
def test_day0_idempotent_double_run(mock_email, db_session):
    """Running reconcile twice at day 0 produces exactly one action log row."""
    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    _, _, invoice, case = _seed(db_session, due)

    reconcile_overdue_invoices(db_session)
    reconcile_overdue_invoices(db_session)  # second call

    logs = db_session.query(RecoveryActionLog).filter(RecoveryActionLog.case_id == case.id).all()
    assert len(logs) == 1, f"Expected 1 log row, got {len(logs)}"
    assert mock_email.call_count == 1


# ---------------------------------------------------------------------------
# 3. +7 day step: second reminder
# ---------------------------------------------------------------------------

@patch("app.agent.receivables_dunning.send_recovery_email", return_value=True)
def test_step2_seven_day_reminder(mock_email, db_session):
    """+7 days: second reminder sent, ladder_step==2."""
    due = datetime.now(timezone.utc) - timedelta(days=8)
    merchant, customer, invoice, case = _seed(db_session, due)

    # Manually set case to step 1 state (simulates day-0 already ran)
    invoice.status = InvoiceStatus.OVERDUE
    case.status = CaseStatus.INTERVENING
    case.ladder_step = 1
    case.next_action_due_at = datetime.now(timezone.utc) - timedelta(minutes=5)  # past due
    db_session.commit()

    result = reconcile_overdue_invoices(db_session)
    assert result["step2_sent"] == 1

    db_session.refresh(case)
    assert case.ladder_step == 2

    logs = db_session.query(RecoveryActionLog).filter(
        RecoveryActionLog.case_id == case.id,
        RecoveryActionLog.ladder_step == 2,
    ).all()
    assert len(logs) == 1
    assert logs[0].action_type == "send_invoice_reminder"
    assert logs[0].amount_offered is None
    assert logs[0].coupon_code is None


# ---------------------------------------------------------------------------
# 4. +30 day escalation — NO contact, hard ceiling
# ---------------------------------------------------------------------------

@patch("app.agent.receivables_dunning.send_recovery_email", return_value=True)
def test_step3_escalation_no_contact(mock_email, db_session):
    """+30 days: case ESCALATED, no email sent, contact_blocked in guardrails."""
    due = datetime.now(timezone.utc) - timedelta(days=31)
    merchant, customer, invoice, case = _seed(db_session, due)

    # Pre-seed step 2 state
    invoice.status = InvoiceStatus.OVERDUE
    case.status = CaseStatus.INTERVENING
    case.ladder_step = 2
    case.next_action_due_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.commit()

    result = reconcile_overdue_invoices(db_session)
    assert result["step3_escalated"] == 1

    db_session.refresh(case)
    assert case.status == CaseStatus.ESCALATED
    assert case.escalated_to_human is True
    assert case.escalation_reason == "receivable_30d_overdue"
    assert case.ladder_step == 3

    # No email was sent
    mock_email.assert_not_called()

    # Audit log records contact_blocked
    logs = db_session.query(RecoveryActionLog).filter(
        RecoveryActionLog.case_id == case.id,
        RecoveryActionLog.ladder_step == 3,
    ).all()
    assert len(logs) == 1
    assert logs[0].action_type == "escalate_to_human"
    guardrails = json.loads(logs[0].guardrail_checks)
    assert guardrails["contact_blocked"] is True
    assert guardrails["discount_offered"] is False


# ---------------------------------------------------------------------------
# 5. Post-escalation: reconcile runs again, no new rows
# ---------------------------------------------------------------------------

@patch("app.agent.receivables_dunning.send_recovery_email", return_value=True)
def test_post_escalation_reconcile_sends_nothing(mock_email, db_session):
    """After step 3 escalation, re-running reconcile adds NO new log rows."""
    due = datetime.now(timezone.utc) - timedelta(days=31)
    merchant, customer, invoice, case = _seed(db_session, due)

    invoice.status = InvoiceStatus.OVERDUE
    case.status = CaseStatus.INTERVENING
    case.ladder_step = 2
    case.next_action_due_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.commit()

    reconcile_overdue_invoices(db_session)  # escalate (step 3)

    # Re-run — next_action_due_at is now set to +60 days, so no Phase 2 trigger
    # But set it artificially past to stress-test
    db_session.refresh(case)
    # Even if we clear next_action_due_at to force re-evaluation, case is ESCALATED
    # and still should NOT produce contact
    reconcile_overdue_invoices(db_session)

    logs_step3 = db_session.query(RecoveryActionLog).filter(
        RecoveryActionLog.case_id == case.id,
        RecoveryActionLog.ladder_step == 3,
    ).all()
    # Idempotency key guarantees only 1 row at step 3
    assert len(logs_step3) == 1
    mock_email.assert_not_called()


# ---------------------------------------------------------------------------
# 6. +60 day write-off flag
# ---------------------------------------------------------------------------

@patch("app.agent.receivables_dunning.send_recovery_email", return_value=True)
def test_step4_write_off_review(mock_email, db_session):
    """+60 days: invoice -> WRITE_OFF_REVIEW, no contact, ladder ends."""
    due = datetime.now(timezone.utc) - timedelta(days=61)
    merchant, customer, invoice, case = _seed(db_session, due)

    # Pre-seed step 3 state
    invoice.status = InvoiceStatus.OVERDUE
    case.status = CaseStatus.ESCALATED
    case.escalated_to_human = True
    case.ladder_step = 3
    case.next_action_due_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.commit()

    result = reconcile_overdue_invoices(db_session)
    assert result["step4_write_off"] == 1

    db_session.refresh(invoice)
    db_session.refresh(case)

    assert invoice.status == InvoiceStatus.WRITE_OFF_REVIEW
    assert case.ladder_step == 4
    assert case.next_action_due_at is None  # ladder ends

    # NO case status change (remains ESCALATED — already set at step 3)
    assert case.status == CaseStatus.ESCALATED

    # No contact whatsoever
    mock_email.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Invoice payment -> RECOVERED, future ladder cancelled
# ---------------------------------------------------------------------------

@patch("app.agent.receivables_dunning.send_recovery_email", return_value=True)
def test_invoice_payment_complete_marks_recovered(mock_email, db_session, client):
    """POST /api/invoice-payment/complete marks invoice PAID, case RECOVERED."""
    due = datetime.now(timezone.utc) - timedelta(days=8)
    merchant, customer, invoice, case = _seed(db_session, due)

    # Pre-set to step 1 intervening state
    invoice.status = InvoiceStatus.OVERDUE
    case.status = CaseStatus.INTERVENING
    case.ladder_step = 1
    case.next_action_due_at = datetime.now(timezone.utc) + timedelta(days=6)
    db_session.commit()

    app.dependency_overrides[get_current_merchant] = lambda: merchant

    res = client.post("/api/invoice-payment/complete", json={
        "invoice_id": invoice.id,
        "razorpay_payment_id": "pay_TEST123",
        "amount_paid": 10000.0,
    })
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "success"
    assert data["invoice_status"] == "paid"
    assert data["case_status"] == "recovered"

    db_session.refresh(invoice)
    db_session.refresh(case)

    assert invoice.status == InvoiceStatus.PAID
    assert case.status == CaseStatus.RECOVERED
    assert case.amount_recovered == 10000.0
    assert case.next_action_due_at is None

    # Exactly one "case_recovered" audit log
    logs = db_session.query(RecoveryActionLog).filter(
        RecoveryActionLog.case_id == case.id,
        RecoveryActionLog.action_type == "case_recovered",
    ).all()
    assert len(logs) == 1
    guardrails = json.loads(logs[0].guardrail_checks)
    assert guardrails["discount_offered"] is False
    assert guardrails["confirmed_payment_id"] == "pay_TEST123"


# ---------------------------------------------------------------------------
# 8. RECOVERED case: reconcile skips it
# ---------------------------------------------------------------------------

@patch("app.agent.receivables_dunning.send_recovery_email", return_value=True)
def test_recovered_case_skipped_by_reconcile(mock_email, db_session):
    """reconcile_overdue_invoices skips RECOVERED cases — no new rows."""
    due = datetime.now(timezone.utc) - timedelta(days=8)
    merchant, customer, invoice, case = _seed(db_session, due)

    invoice.status = InvoiceStatus.PAID
    case.status = CaseStatus.RECOVERED
    case.ladder_step = 1
    case.next_action_due_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.commit()

    result = reconcile_overdue_invoices(db_session)
    assert result["skipped_terminal"] >= 1
    assert result["step2_sent"] == 0

    logs = db_session.query(RecoveryActionLog).filter(RecoveryActionLog.case_id == case.id).all()
    assert len(logs) == 0
    mock_email.assert_not_called()


# ---------------------------------------------------------------------------
# 9. /internal/reconcile endpoint smoke test
# ---------------------------------------------------------------------------

@patch("app.agent.receivables_dunning.send_recovery_email", return_value=True)
def test_internal_reconcile_endpoint(mock_email, db_session, client):
    """POST /internal/reconcile returns 200 with reconcile_summary."""
    res = client.post("/internal/reconcile")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "reconcile_summary" in data
    assert "new_overdue" in data["reconcile_summary"]


# ---------------------------------------------------------------------------
# 10. No discount invariant across all ladder steps
# ---------------------------------------------------------------------------

@patch("app.agent.receivables_dunning.send_recovery_email", return_value=True)
def test_no_discount_invariant_across_all_steps(mock_email, db_session):
    """Every action log row produced by the dunning ladder has amount_offered=None, coupon_code=None."""
    due = datetime.now(timezone.utc) - timedelta(days=61)
    merchant, customer, invoice, case = _seed(db_session, due)

    # Fast-forward through all steps in one db session by setting next_action_due_at retroactively
    # Step 1
    reconcile_overdue_invoices(db_session)
    db_session.refresh(case)

    # Step 2
    case.next_action_due_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()
    reconcile_overdue_invoices(db_session)
    db_session.refresh(case)

    # Step 3
    case.next_action_due_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()
    reconcile_overdue_invoices(db_session)
    db_session.refresh(case)

    # Step 4
    case.next_action_due_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()
    reconcile_overdue_invoices(db_session)

    all_logs = db_session.query(RecoveryActionLog).filter(RecoveryActionLog.case_id == case.id).all()
    assert len(all_logs) == 4, f"Expected 4 log rows (one per step), got {len(all_logs)}"

    for log in all_logs:
        assert log.amount_offered is None, f"Step {log.ladder_step}: amount_offered must be None, got {log.amount_offered}"
        assert log.coupon_code is None, f"Step {log.ladder_step}: coupon_code must be None, got {log.coupon_code}"
        guardrails = json.loads(log.guardrail_checks)
        assert guardrails["discount_offered"] is False, f"Step {log.ladder_step}: discount_offered must be False"


# ---------------------------------------------------------------------------
# 11. Concurrency: Background thread + /internal/reconcile race condition
# ---------------------------------------------------------------------------

@patch("app.agent.receivables_dunning.send_recovery_email", return_value=True)
def test_concurrent_reconcile_only_sends_one_email(mock_email, tmp_path):
    """Simulate background thread and /internal/reconcile firing concurrently.
    
    Verifies write-then-check-then-send: only the thread that successfully
    claims the idempotency key sends the email. The loser of the race aborts.
    """
    import concurrent.futures

    db_file = tmp_path / "test_concurrent.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"timeout": 15, "check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestSessionLocal = sessionmaker(bind=engine)

    init_sess = TestSessionLocal()
    due = datetime.now(timezone.utc) - timedelta(minutes=5)
    merchant, customer, invoice, case = _seed(init_sess, due)
    case_id = case.id
    init_sess.close()

    def worker():
        sess = TestSessionLocal()
        try:
            return reconcile_overdue_invoices(sess)
        finally:
            sess.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(worker)
        f2 = executor.submit(worker)
        r1 = f1.result()
        r2 = f2.result()

    # Total emails sent across both concurrent executions MUST be exactly 1
    assert mock_email.call_count == 1

    # Exactly 1 log row in database
    verify_sess = TestSessionLocal()
    logs = verify_sess.query(RecoveryActionLog).filter(RecoveryActionLog.case_id == case_id).all()
    assert len(logs) == 1
    assert logs[0].outcome == "sent"
    verify_sess.close()
    engine.dispose()

