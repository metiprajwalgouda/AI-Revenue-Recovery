"""
Tests for Revenue Intelligence unified cross-scenario rollup (/api/merchant/advanced-metrics).
Verifies:
1. Sourcing from RecoveryCase + RecoveryActionLog + Invoice across all 3 scenarios:
   - PAYMENT_FAILURE
   - CHECKOUT_ABANDONMENT
   - OVERDUE_RECEIVABLE
2. Strict isolation: Legacy CheckoutSession and RecoveryOutcomeRecord rows are NOT read into dashboard figures.
3. Accurate financials, funnel counts, multi-scenario cohorts, and audit trail.
"""

import json
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db_models import (
    Base, MerchantUser, CustomerUser, CheckoutSession, SessionStatus,
    RecoveryOutcomeRecord, Invoice, InvoiceStatus,
    RecoveryCase, RecoveryActionLog, RecoveryScenario, CaseStatus,
)
from app.db import get_db
from app.main import app
from app.merchant_auth_routes import get_current_merchant


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


def test_advanced_metrics_unified_rollup_and_legacy_isolation(client, db_session):
    # 1. Setup Merchant and Customers
    merchant = MerchantUser(
        email="merchant_rollup@example.com",
        password_hash="pw_hash",
        store_name="Unified Store",
    )
    other_merchant = MerchantUser(
        email="other_merchant@example.com",
        password_hash="pw_hash",
        store_name="Other Store",
    )
    customer = CustomerUser(
        email="cust_rollup@example.com",
        password_hash="pw_hash",
        name="Charlie Customer",
        phone="+919876500000",
    )
    db_session.add_all([merchant, other_merchant, customer])
    db_session.commit()
    db_session.refresh(merchant)
    db_session.refresh(other_merchant)
    db_session.refresh(customer)

    # 2. Add POISON / LEGACY records that MUST NOT be included
    legacy_session = CheckoutSession(
        event_id="legacy_poison_evt",
        customer_user_id=customer.id,
        customer_name="Legacy Fake",
        customer_email="fake@legacy.com",
        customer_phone="+919876543210",
        cart_value=999999.0,
        status=SessionStatus.COMPLETED,
    )
    db_session.add(legacy_session)
    db_session.flush()

    legacy_outcome = RecoveryOutcomeRecord(
        session_id=legacy_session.id,
        predicted_reason="legacy_fake_reason",
        confidence=0.99,
        classification_method="rule",
        action_taken="legacy_action",
        action_success=True,
        delivery_status="sent",
        confirmed_recovered_amount=888888.0,
        amount_offered=111111.0,
    )
    db_session.add(legacy_outcome)

    # Add other merchant's case that MUST NOT leak
    other_case = RecoveryCase(
        merchant_id=other_merchant.id,
        customer_user_id=customer.id,
        scenario=RecoveryScenario.PAYMENT_FAILURE,
        amount_at_risk=77777.0,
        amount_recovered=77777.0,
        status=CaseStatus.RECOVERED,
        classification="insufficient_funds",
    )
    db_session.add(other_case)
    db_session.commit()

    # 3. Create Real Multi-Scenario Cases for current_merchant
    now = datetime.now(timezone.utc)

    # Case 1: Payment Failure - RECOVERED (amount 3000, recovered 3000, confirmed discount 200)
    case_pf_rec = RecoveryCase(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        scenario=RecoveryScenario.PAYMENT_FAILURE,
        amount_at_risk=3000.0,
        amount_recovered=3000.0,
        discount_amount=200.0,
        coupon_code_used="SAVE200",
        status=CaseStatus.RECOVERED,
        classification="insufficient_funds",
        created_at=now - timedelta(hours=5),
    )
    db_session.add(case_pf_rec)
    db_session.flush()

    log1 = RecoveryActionLog(
        case_id=case_pf_rec.id,
        idempotency_key="pf_rec:1",
        ladder_step=1,
        action_type="send_payment_link",
        outcome="sent",
        reason="Offered link",
        created_at=now - timedelta(hours=4),
    )
    log2 = RecoveryActionLog(
        case_id=case_pf_rec.id,
        idempotency_key="pf_rec:2",
        ladder_step=1,
        action_type="case_recovered",
        outcome="sent",
        reason="Payment captured via Razorpay",
        created_at=now - timedelta(hours=3),
    )
    db_session.add_all([log1, log2])

    # Case 2: Checkout Abandonment - INTERVENING (amount 2000, at risk 2000, coupon discount 200)
    case_ab_interv = RecoveryCase(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
        amount_at_risk=2000.0,
        amount_recovered=0.0,
        status=CaseStatus.INTERVENING,
        classification="price_sensitivity",
        created_at=now - timedelta(hours=2),
    )
    db_session.add(case_ab_interv)
    db_session.flush()

    log3 = RecoveryActionLog(
        case_id=case_ab_interv.id,
        idempotency_key="ab_interv:1",
        ladder_step=1,
        action_type="issue_coupon",
        amount_offered=200.0,
        outcome="sent",
        reason="Sent 10% discount coupon",
        created_at=now - timedelta(hours=1),
    )
    db_session.add(log3)

    # Case 3: Overdue Receivable - SUPPRESSED / ESCALATED (amount 5000, at risk 5000, escalated_to_human=True)
    inv = Invoice(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        invoice_number="INV-ROLLUP-001",
        amount=5000.0,
        due_date=now - timedelta(days=31),
        status=InvoiceStatus.OVERDUE,
    )
    db_session.add(inv)
    db_session.flush()

    case_rec_esc = RecoveryCase(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        invoice_id=inv.id,
        scenario=RecoveryScenario.OVERDUE_RECEIVABLE,
        amount_at_risk=5000.0,
        amount_recovered=0.0,
        status=CaseStatus.ESCALATED,
        escalated_to_human=True,
        classification="receivable_30d_overdue",
        created_at=now - timedelta(days=32),
    )
    db_session.add(case_rec_esc)
    db_session.flush()

    log4 = RecoveryActionLog(
        case_id=case_rec_esc.id,
        idempotency_key="rec_esc:1",
        ladder_step=3,
        action_type="escalate_to_human",
        outcome="suppressed",
        reason="Autonomous contact stopped at 30d threshold",
        created_at=now - timedelta(days=1),
    )
    db_session.add(log4)


    # Case 4: Payment Failure - LOST (amount 1500, contact_touches=3)
    case_pf_lost = RecoveryCase(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        scenario=RecoveryScenario.PAYMENT_FAILURE,
        amount_at_risk=1500.0,
        amount_recovered=0.0,
        status=CaseStatus.LOST,
        contact_touches=3,
        classification="bank_decline",
        created_at=now - timedelta(hours=10),
    )
    db_session.add(case_pf_lost)
    db_session.commit()

    # 4. Invoke GET /api/merchant/advanced-metrics
    app.dependency_overrides[get_current_merchant] = lambda: merchant

    res = client.get("/api/merchant/advanced-metrics")
    assert res.status_code == 200, res.text
    data = res.json()

    # 5. Financials Assertions
    fin = data["financials"]
    assert fin["total_recovered"] == 3000.0
    assert fin["total_at_risk"] == 7000.0
    assert fin["total_lost"] == 1500.0
    assert fin["cost_of_discounts"] == 200.0
    assert fin["net_revenue_add"] == 2800.0

    # Ensure legacy poison figures NEVER leak into financials
    assert fin["total_recovered"] < 10000.0
    assert fin["total_at_risk"] < 10000.0

    # 6. Funnel Assertions
    funnel = data["funnel"]
    assert funnel["total_cases_detected"] == 4
    assert funnel["successful_recoveries"] == 1
    assert funnel["circuit_breaker_trips"] == 1
    assert funnel["escalated_to_human_count"] == 1
    assert funnel["suppressed_by_guardrails"] == 1
    assert funnel["interventions_attempted"] >= 2

    # 7. Cohorts Assertions
    cohorts = data["cohorts"]
    assert "insufficient_funds" in cohorts
    assert cohorts["insufficient_funds"]["count"] == 1
    assert cohorts["insufficient_funds"]["recovered"] == 1
    assert cohorts["insufficient_funds"]["value"] == 3000.0
    assert cohorts["insufficient_funds"]["scenario"] == "payment_failure"

    assert "price_sensitivity" in cohorts
    assert cohorts["price_sensitivity"]["count"] == 1
    assert cohorts["price_sensitivity"]["recovered"] == 0
    assert cohorts["price_sensitivity"]["scenario"] == "checkout_abandonment"

    assert "receivable_30d_overdue" in cohorts
    assert cohorts["receivable_30d_overdue"]["count"] == 1
    assert cohorts["receivable_30d_overdue"]["recovered"] == 0
    assert cohorts["receivable_30d_overdue"]["scenario"] == "overdue_receivable"

    assert "bank_decline" in cohorts
    assert cohorts["bank_decline"]["count"] == 1

    # Legacy fake reason must NOT exist in cohorts
    assert "legacy_fake_reason" not in cohorts

    # 8. Audit Logs Assertions
    audit_logs = data["audit_logs"]
    assert len(audit_logs) == 4
    assert all("legacy" not in log["session_id"].lower() for log in audit_logs)
    actions = [log["action"] for log in audit_logs]
    assert "send_payment_link" in actions
    assert "case_recovered" in actions
    assert "issue_coupon" in actions
    assert "escalate_to_human" in actions
