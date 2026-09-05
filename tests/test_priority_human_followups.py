"""
Tests for Human Follow-ups & Priority Escalations (all 3 scenarios, pending approvals, mark recovered, merchant isolation).

Coverage:
  1. GET /merchant/priority: HTML page rendering and authentication guard.
  2. GET /api/merchant/priority: Unified queue with escalated cases + pending coupon approvals across all 3 scenarios.
  3. Server-side filtering by scenario, item type, and status.
  4. Approve/Reject pending coupon actions via existing endpoints.
  5. Mark Recovered action on escalated cases (RecoveryCase + Invoice status update + RecoveryActionLog).
  6. Strict per-merchant data isolation (sentinel test).
"""

import json
import pytest
from datetime import datetime, timezone, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db_models import (
    Base, MerchantUser, CustomerUser,
    CheckoutSession, SessionStatus,
    Invoice, InvoiceStatus,
    RecoveryCase, RecoveryActionLog,
    RecoveryScenario, CaseStatus,
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
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def seed_followups_data(db_session):
    now = datetime.now(timezone.utc)

    # Merchant 1
    m1 = MerchantUser(
        email="merchant_m1@example.com",
        password_hash="hash1",
        store_name="Store 1",
    )
    # Merchant 2 (Sentinel)
    m2 = MerchantUser(
        email="merchant_m2@example.com",
        password_hash="hash2",
        store_name="Store 2 Sentinel",
    )
    # Customers
    c1 = CustomerUser(email="alice@example.com", password_hash="h1", name="Alice Shopper", phone="+919876543210")
    c2 = CustomerUser(email="bob@example.com", password_hash="h2", name="Bob Builder", phone="+919876543211")
    c3 = CustomerUser(email="carol@example.com", password_hash="h3", name="Carol Corporate", phone="+919876543212")

    db_session.add_all([m1, m2, c1, c2, c3])
    db_session.flush()

    # --- Scenario 1: CHECKOUT_ABANDONMENT ---
    # 1.1 Escalated Abandonment Case
    s1 = CheckoutSession(
        event_id="chk_ab_esc_1", customer_user_id=c1.id, customer_name=c1.name,
        customer_email=c1.email, customer_phone=c1.phone, cart_value=4000.0, status=SessionStatus.ABANDONED
    )
    db_session.add(s1)
    db_session.flush()

    case_ab_esc = RecoveryCase(
        merchant_id=m1.id, customer_user_id=c1.id, checkout_session_id=s1.id,
        scenario=RecoveryScenario.CHECKOUT_ABANDONMENT, amount_at_risk=4000.0,
        status=CaseStatus.ESCALATED, escalated_to_human=True, escalation_reason="vip_cart_abandonment",
        classification="price_sensitivity", ladder_step=1,
    )
    db_session.add(case_ab_esc)

    # 1.2 Pending Approval Abandonment Case
    s2 = CheckoutSession(
        event_id="chk_ab_pend_2", customer_user_id=c2.id, customer_name=c2.name,
        customer_email=c2.email, customer_phone=c2.phone, cart_value=5000.0, status=SessionStatus.ABANDONED
    )
    db_session.add(s2)
    db_session.flush()

    case_ab_pend = RecoveryCase(
        merchant_id=m1.id, customer_user_id=c2.id, checkout_session_id=s2.id,
        scenario=RecoveryScenario.CHECKOUT_ABANDONMENT, amount_at_risk=5000.0,
        status=CaseStatus.INTERVENING, escalated_to_human=False,
        classification="price_sensitivity", ladder_step=1,
    )
    db_session.add(case_ab_pend)
    db_session.flush()

    log_ab_pend = RecoveryActionLog(
        case_id=case_ab_pend.id, idempotency_key=f"{case_ab_pend.id}:1:pending_discount",
        ladder_step=1, action_type="email", reason="Offer 10% discount on high value cart",
        outcome="pending_approval", amount_offered=500.0, coupon_code="OFF10", requires_human_approval=True,
    )
    db_session.add(log_ab_pend)

    # --- Scenario 2: PAYMENT_FAILURE ---
    # 2.1 Escalated Payment Failure Case
    case_pf_esc = RecoveryCase(
        merchant_id=m1.id, customer_user_id=c1.id,
        scenario=RecoveryScenario.PAYMENT_FAILURE, amount_at_risk=8500.0,
        status=CaseStatus.ESCALATED, escalated_to_human=True, escalation_reason="risk_terminal_decline",
        classification="risk_block", ladder_step=1,
    )
    db_session.add(case_pf_esc)

    # 2.2 Pending Approval Payment Failure Case
    case_pf_pend = RecoveryCase(
        merchant_id=m1.id, customer_user_id=c2.id,
        scenario=RecoveryScenario.PAYMENT_FAILURE, amount_at_risk=6000.0,
        status=CaseStatus.INTERVENING, escalated_to_human=False,
        classification="card_declined", ladder_step=1,
    )
    db_session.add(case_pf_pend)
    db_session.flush()

    log_pf_pend = RecoveryActionLog(
        case_id=case_pf_pend.id, idempotency_key=f"{case_pf_pend.id}:1:pending_pf_discount",
        ladder_step=1, action_type="email", reason="Offer alternative payment method with 5% discount",
        outcome="pending_approval", amount_offered=300.0, coupon_code="PF5", requires_human_approval=True,
    )
    db_session.add(log_pf_pend)

    # --- Scenario 3: OVERDUE_RECEIVABLE ---
    # 3.1 Escalated Invoice Case
    inv1 = Invoice(
        merchant_id=m1.id, customer_user_id=c3.id, invoice_number="INV-2026-ESC1",
        amount=20000.0, currency="INR", issue_date=now - timedelta(days=40),
        due_date=now - timedelta(days=31), status=InvoiceStatus.OVERDUE,
    )
    db_session.add(inv1)
    db_session.flush()

    case_rec_esc = RecoveryCase(
        merchant_id=m1.id, customer_user_id=c3.id, invoice_id=inv1.id,
        scenario=RecoveryScenario.OVERDUE_RECEIVABLE, amount_at_risk=20000.0,
        status=CaseStatus.ESCALATED, escalated_to_human=True, escalation_reason="receivable_30d_overdue",
        ladder_step=3,
    )
    db_session.add(case_rec_esc)

    # 3.2 Pending Approval Invoice Case
    inv2 = Invoice(
        merchant_id=m1.id, customer_user_id=c3.id, invoice_number="INV-2026-PEND2",
        amount=15000.0, currency="INR", issue_date=now - timedelta(days=20),
        due_date=now - timedelta(days=10), status=InvoiceStatus.OVERDUE,
    )
    db_session.add(inv2)
    db_session.flush()

    case_rec_pend = RecoveryCase(
        merchant_id=m1.id, customer_user_id=c3.id, invoice_id=inv2.id,
        scenario=RecoveryScenario.OVERDUE_RECEIVABLE, amount_at_risk=15000.0,
        status=CaseStatus.INTERVENING, escalated_to_human=False,
        ladder_step=2,
    )
    db_session.add(case_rec_pend)
    db_session.flush()

    log_rec_pend = RecoveryActionLog(
        case_id=case_rec_pend.id, idempotency_key=f"{case_rec_pend.id}:2:pending_rec_discount",
        ladder_step=2, action_type="send_invoice_reminder", reason="Offer settlement discount for prompt invoice clearing",
        outcome="pending_approval", amount_offered=13500.0, coupon_code="SETTLE10", requires_human_approval=True,
    )
    db_session.add(log_rec_pend)

    # --- Merchant 2 (Sentinel Items) ---
    case_m2_esc = RecoveryCase(
        merchant_id=m2.id, customer_user_id=c1.id,
        scenario=RecoveryScenario.CHECKOUT_ABANDONMENT, amount_at_risk=99999.0,
        status=CaseStatus.ESCALATED, escalated_to_human=True, escalation_reason="m2_vip_case",
    )
    case_m2_pend = RecoveryCase(
        merchant_id=m2.id, customer_user_id=c1.id,
        scenario=RecoveryScenario.OVERDUE_RECEIVABLE, amount_at_risk=88888.0,
        status=CaseStatus.INTERVENING, escalated_to_human=False,
    )
    db_session.add_all([case_m2_esc, case_m2_pend])
    db_session.flush()

    log_m2_pend = RecoveryActionLog(
        case_id=case_m2_pend.id, idempotency_key=f"{case_m2_pend.id}:1:m2_pending_rec",
        ladder_step=1, action_type="email", reason="M2 isolated action",
        outcome="pending_approval", amount_offered=8888.0, requires_human_approval=True,
    )
    db_session.add(log_m2_pend)

    db_session.commit()
    return {
        "m1": m1, "m2": m2, "c1": c1, "c2": c2, "c3": c3,
        "case_ab_esc": case_ab_esc, "case_ab_pend": case_ab_pend, "log_ab_pend": log_ab_pend,
        "case_pf_esc": case_pf_esc, "case_pf_pend": case_pf_pend, "log_pf_pend": log_pf_pend,
        "case_rec_esc": case_rec_esc, "case_rec_pend": case_rec_pend, "log_rec_pend": log_rec_pend,
        "inv1": inv1, "inv2": inv2,
        "case_m2_esc": case_m2_esc, "case_m2_pend": case_m2_pend, "log_m2_pend": log_m2_pend,
    }


def test_merchant_priority_html_page_auth(client, seed_followups_data):
    """Confirm /merchant/priority page returns 200 when authenticated and redirects when unauthenticated."""
    from app.auth import create_merchant_session_token, MERCHANT_COOKIE_NAME
    m1 = seed_followups_data["m1"]

    # Unauthenticated -> redirect to /merchant/login
    res_unauth = client.get("/merchant/priority", follow_redirects=False)
    assert res_unauth.status_code in (302, 307)
    assert "/merchant/login" in res_unauth.headers.get("location", "")

    # Authenticated via cookie
    token = create_merchant_session_token(m1.id)
    client.cookies.set(MERCHANT_COOKIE_NAME, token)
    res_auth = client.get("/merchant/priority")
    assert res_auth.status_code == 200
    assert "Human Follow-ups" in res_auth.text
    assert "scenario-filter" in res_auth.text
    assert "type-filter" in res_auth.text
    assert "status-filter" in res_auth.text


def test_get_priority_all_scenarios_and_pending_approvals(client, seed_followups_data):
    """Confirm /api/merchant/priority returns all escalated cases and pending coupon approvals for M1."""
    m1 = seed_followups_data["m1"]
    app.dependency_overrides[get_current_merchant] = lambda: m1

    res = client.get("/api/merchant/priority")
    assert res.status_code == 200
    data = res.json()

    # M1 has 3 escalated cases + 3 pending approvals = 6 total items
    assert len(data) == 6

    # Verify all 3 scenarios are present
    scenarios = {item["scenario"] for item in data}
    assert "checkout_abandonment" in scenarios
    assert "payment_failure" in scenarios
    assert "overdue_receivable" in scenarios

    # Verify both item types are present
    types = {item["item_type"] for item in data}
    assert "escalated_case" in types
    assert "pending_approval" in types

    # Verify pending approvals contain offer details
    pending_items = [i for i in data if i["item_type"] == "pending_approval"]
    assert len(pending_items) == 3
    coupon_codes = {p.get("coupon_code") for p in pending_items}
    assert "OFF10" in coupon_codes
    assert "PF5" in coupon_codes
    assert "SETTLE10" in coupon_codes

    # Verify M2 sentinel items (99999, 88888) are NEVER returned
    all_amounts = [i.get("cart_value") or i.get("amount_at_risk") for i in data]
    assert 99999.0 not in all_amounts
    assert 88888.0 not in all_amounts


def test_priority_filter_params(client, seed_followups_data):
    """Confirm server-side filtering by scenario, type, and status works correctly."""
    m1 = seed_followups_data["m1"]
    app.dependency_overrides[get_current_merchant] = lambda: m1

    # Filter by type=pending_approval
    res_pend = client.get("/api/merchant/priority?type=pending_approval")
    assert res_pend.status_code == 200
    assert len(res_pend.json()) == 3
    assert all(i["item_type"] == "pending_approval" for i in res_pend.json())

    # Filter by type=escalated_case
    res_esc = client.get("/api/merchant/priority?type=escalated_case")
    assert res_esc.status_code == 200
    assert len(res_esc.json()) == 3
    assert all(i["item_type"] == "escalated_case" for i in res_esc.json())

    # Filter by scenario=payment_failure
    res_pf = client.get("/api/merchant/priority?scenario=payment_failure")
    assert res_pf.status_code == 200
    assert len(res_pf.json()) == 2

    # Filter by scenario=overdue_receivable and type=pending_approval
    res_rec_pend = client.get("/api/merchant/priority?scenario=overdue_receivable&type=pending_approval")
    assert res_rec_pend.status_code == 200
    assert len(res_rec_pend.json()) == 1
    assert res_rec_pend.json()[0]["coupon_code"] == "SETTLE10"


def test_approve_and_reject_actions(client, seed_followups_data, db_session):
    """Confirm Approve and Reject endpoints work on pending approval items and update the priority list."""
    m1 = seed_followups_data["m1"]
    log_ab_pend = seed_followups_data["log_ab_pend"]
    log_pf_pend = seed_followups_data["log_pf_pend"]
    app.dependency_overrides[get_current_merchant] = lambda: m1

    # 1. Approve log_ab_pend
    res_appr = client.post(f"/api/merchant/recovery-actions/{log_ab_pend.id}/approve")
    assert res_appr.status_code == 200
    assert res_appr.json()["status"] == "success"
    assert res_appr.json()["outcome"] == "approved"

    # Verify log in DB
    db_session.expire(log_ab_pend)
    assert log_ab_pend.outcome == "approved"
    assert log_ab_pend.approved_by == m1.id

    # 2. Reject log_pf_pend
    res_rej = client.post(f"/api/merchant/recovery-actions/{log_pf_pend.id}/reject")
    assert res_rej.status_code == 200
    assert res_rej.json()["status"] == "success"
    assert res_rej.json()["outcome"] == "rejected"

    db_session.expire(log_pf_pend)
    assert log_pf_pend.outcome == "rejected"

    # 3. GET /api/merchant/priority should now only return 4 items (1 pending approval left + 3 escalated)
    res_updated = client.get("/api/merchant/priority")
    assert res_updated.status_code == 200
    assert len(res_updated.json()) == 4
    pending_left = [i for i in res_updated.json() if i["item_type"] == "pending_approval"]
    assert len(pending_left) == 1
    assert pending_left[0]["coupon_code"] == "SETTLE10"


def test_mark_recovered_action(client, seed_followups_data, db_session):
    """Confirm Mark Recovered updates RecoveryCase, Invoice, CheckoutSession, and writes RecoveryActionLog."""
    m1 = seed_followups_data["m1"]
    case_ab_esc = seed_followups_data["case_ab_esc"]
    case_rec_esc = seed_followups_data["case_rec_esc"]
    inv1 = seed_followups_data["inv1"]
    app.dependency_overrides[get_current_merchant] = lambda: m1

    # 1. Mark Abandonment Case as Recovered
    res1 = client.post(f"/api/merchant/priority/case_{case_ab_esc.id}/recovered", json={
        "amount_recovered": 4000.0,
        "reason": "Customer completed order over phone"
    })
    assert res1.status_code == 200
    assert res1.json()["status"] == "success"
    assert res1.json()["amount_recovered"] == 4000.0

    db_session.expire(case_ab_esc)
    assert case_ab_esc.status == CaseStatus.RECOVERED
    assert case_ab_esc.amount_recovered == 4000.0
    assert case_ab_esc.escalated_to_human is False

    # Check action log was written
    log_rec = db_session.query(RecoveryActionLog).filter(
        RecoveryActionLog.case_id == case_ab_esc.id,
        RecoveryActionLog.action_type == "human_marked_recovered"
    ).first()
    assert log_rec is not None
    assert log_rec.approved_by == m1.id
    assert "Customer completed order over phone" in log_rec.reason

    # 2. Mark Overdue Receivable Case as Recovered
    res2 = client.post(f"/api/merchant/priority/case_{case_rec_esc.id}/recovered", json={
        "amount_recovered": 20000.0,
        "reason": "Direct bank wire received"
    })
    assert res2.status_code == 200
    db_session.expire(case_rec_esc)
    db_session.expire(inv1)
    assert case_rec_esc.status == CaseStatus.RECOVERED
    assert inv1.status == InvoiceStatus.PAID


def test_strict_merchant_isolation(client, seed_followups_data):
    """Confirm Merchant 2 cannot view or modify Merchant 1's follow-ups or pending approvals."""
    m2 = seed_followups_data["m2"]
    log_rec_pend = seed_followups_data["log_rec_pend"]  # M1's pending approval
    case_ab_esc = seed_followups_data["case_ab_esc"]    # M1's escalated case

    app.dependency_overrides[get_current_merchant] = lambda: m2

    # 1. M2 queue only contains M2's 2 items
    res = client.get("/api/merchant/priority")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 2
    amounts = [i.get("cart_value") or i.get("amount_at_risk") for i in data]
    assert 99999.0 in amounts
    assert 88888.0 in amounts

    # 2. M2 attempting to approve M1's pending log returns 403 Forbidden
    res_appr_m1 = client.post(f"/api/merchant/recovery-actions/{log_rec_pend.id}/approve")
    assert res_appr_m1.status_code == 403

    # 3. M2 attempting to reject M1's pending log returns 403 Forbidden
    res_rej_m1 = client.post(f"/api/merchant/recovery-actions/{log_rec_pend.id}/reject")
    assert res_rej_m1.status_code == 403

    # 4. M2 attempting to mark recovered on M1's case returns 403 Forbidden
    res_recov_m1 = client.post(f"/api/merchant/priority/case_{case_ab_esc.id}/recovered", json={"amount_recovered": 4000.0})
    assert res_recov_m1.status_code == 403

    # 5. M2 attempting to mark lost on M1's case returns 403 Forbidden
    res_lost_m1 = client.post(f"/api/merchant/priority/case_{case_ab_esc.id}/lost", json={"reason": "lost"})
    assert res_lost_m1.status_code == 403
