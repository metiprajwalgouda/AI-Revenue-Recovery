"""
Tests for Overdue Receivables tab, API endpoints, and advanced-metrics integration.

Coverage:
  1. GET /merchant/receivables: HTML page rendering and authentication guard.
  2. GET /api/merchant/receivables: Summary KPIs (At Risk, Recovered, Lost, Rate %, Escalated, DSO)
     and cases table payload.
  3. GET /api/merchant/receivables/{case_id}/audit-trail: Chronological logs with guardrail breakdown.
  4. Strict merchant scoping: Merchant isolation on list and audit trail.
  5. GET /api/merchant/advanced-metrics: Real overdue_receivable breakdown matching merchant's cases.
"""

import os
import json
import pytest
from datetime import datetime, timezone, timedelta

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
from app.merchant_auth_routes import get_current_merchant, get_current_merchant_or_none


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
def seed_receivables(db_session):
    now = datetime.now(timezone.utc)

    # Merchant 1
    m1 = MerchantUser(
        email="merchant_rec1@example.com",
        password_hash="hash1",
        store_name="Receivables Store 1",
    )
    # Merchant 2
    m2 = MerchantUser(
        email="merchant_rec2@example.com",
        password_hash="hash2",
        store_name="Receivables Store 2",
    )
    # Customer
    c1 = CustomerUser(
        email="cust_rec1@example.com",
        password_hash="hashc",
        name="Bob Buyer",
        phone="+919876543211",
    )
    db_session.add_all([m1, m2, c1])
    db_session.flush()

    # Case 1 (M1): Active overdue invoice (Step 1)
    inv1 = Invoice(
        merchant_id=m1.id,
        customer_user_id=c1.id,
        invoice_number="INV-2026-001",
        amount=5000.0,
        currency="INR",
        issue_date=now - timedelta(days=10),
        due_date=now - timedelta(days=3),
        status=InvoiceStatus.OVERDUE,
    )
    db_session.add(inv1)
    db_session.flush()

    case1 = RecoveryCase(
        merchant_id=m1.id,
        customer_user_id=c1.id,
        invoice_id=inv1.id,
        scenario=RecoveryScenario.OVERDUE_RECEIVABLE,
        amount_at_risk=5000.0,
        amount_recovered=0.0,
        status=CaseStatus.INTERVENING,
        ladder_step=1,
        last_action_at=now - timedelta(days=3),
    )
    db_session.add(case1)
    db_session.flush()

    log1 = RecoveryActionLog(
        case_id=case1.id,
        idempotency_key=f"{case1.id}:1:send_invoice_reminder",
        ladder_step=1,
        action_type="send_invoice_reminder",
        reason="Invoice past due — Day 0 reminder sent.",
        guardrail_checks=json.dumps({"ladder_step": 1, "contact_made": True, "discount_offered": False}),
        outcome="sent",
    )
    db_session.add(log1)

    # Case 2 (M1): Recovered invoice
    inv2 = Invoice(
        merchant_id=m1.id,
        customer_user_id=c1.id,
        invoice_number="INV-2026-002",
        amount=3000.0,
        currency="INR",
        issue_date=now - timedelta(days=15),
        due_date=now - timedelta(days=8),
        status=InvoiceStatus.PAID,
    )
    db_session.add(inv2)
    db_session.flush()

    case2 = RecoveryCase(
        merchant_id=m1.id,
        customer_user_id=c1.id,
        invoice_id=inv2.id,
        scenario=RecoveryScenario.OVERDUE_RECEIVABLE,
        amount_at_risk=3000.0,
        amount_recovered=3000.0,
        status=CaseStatus.RECOVERED,
        ladder_step=1,
        last_action_at=now - timedelta(days=7),
    )
    db_session.add(case2)

    # Case 3 (M1): Escalated invoice (Step 3)
    inv3 = Invoice(
        merchant_id=m1.id,
        customer_user_id=c1.id,
        invoice_number="INV-2026-003",
        amount=12000.0,
        currency="INR",
        issue_date=now - timedelta(days=40),
        due_date=now - timedelta(days=32),
        status=InvoiceStatus.OVERDUE,
    )
    db_session.add(inv3)
    db_session.flush()

    case3 = RecoveryCase(
        merchant_id=m1.id,
        customer_user_id=c1.id,
        invoice_id=inv3.id,
        scenario=RecoveryScenario.OVERDUE_RECEIVABLE,
        amount_at_risk=12000.0,
        amount_recovered=0.0,
        status=CaseStatus.ESCALATED,
        escalated_to_human=True,
        escalation_reason="receivable_30d_overdue",
        ladder_step=3,
        last_action_at=now - timedelta(days=2),
    )
    db_session.add(case3)

    # Case 4 (M2): Belongs to Merchant 2
    inv4 = Invoice(
        merchant_id=m2.id,
        customer_user_id=c1.id,
        invoice_number="INV-M2-001",
        amount=8000.0,
        currency="INR",
        issue_date=now - timedelta(days=5),
        due_date=now - timedelta(days=1),
        status=InvoiceStatus.OVERDUE,
    )
    db_session.add(inv4)
    db_session.flush()

    case4 = RecoveryCase(
        merchant_id=m2.id,
        customer_user_id=c1.id,
        invoice_id=inv4.id,
        scenario=RecoveryScenario.OVERDUE_RECEIVABLE,
        amount_at_risk=8000.0,
        amount_recovered=0.0,
        status=CaseStatus.INTERVENING,
        ladder_step=1,
    )
    db_session.add(case4)

    db_session.commit()
    return {"m1": m1, "m2": m2, "c1": c1, "case1": case1, "case2": case2, "case3": case3, "case4": case4}


def test_merchant_receivables_html_page_auth(client, seed_receivables):
    """Confirm /merchant/receivables page returns 200 when authenticated and redirects when not."""
    from app.auth import create_merchant_session_token, MERCHANT_COOKIE_NAME
    m1 = seed_receivables["m1"]

    # Unauthenticated -> redirect to /merchant/login
    res_unauth = client.get("/merchant/receivables", follow_redirects=False)
    assert res_unauth.status_code == 307 or res_unauth.status_code == 302
    assert "/merchant/login" in res_unauth.headers.get("location", "")

    # Authenticated via session cookie
    token = create_merchant_session_token(m1.id)
    client.cookies.set(MERCHANT_COOKIE_NAME, token)
    res_auth = client.get("/merchant/receivables")
    assert res_auth.status_code == 200
    assert "Overdue Receivables" in res_auth.text
    assert "sidebar-link" in res_auth.text


def test_get_merchant_receivables_kpi_and_cases(client, seed_receivables):
    """Confirm /api/merchant/receivables computes accurate KPIs and returns case list."""
    m1 = seed_receivables["m1"]
    app.dependency_overrides[get_current_merchant] = lambda: m1

    res = client.get("/api/merchant/receivables")
    assert res.status_code == 200
    data = res.json()

    assert "summary" in data
    assert "cases" in data

    s = data["summary"]
    # M1 has 3 cases: Case 1 (5000 at risk), Case 2 (3000 recovered), Case 3 (12000 at risk)
    # Total At Risk (open cases): 5000 + 12000 = 17000
    assert s["total_at_risk"] == 17000.0
    # Total Recovered: 3000
    assert s["total_recovered"] == 3000.0
    assert s["total_cases"] == 3
    assert s["recovered_cases"] == 1
    assert round(s["recovery_rate_pct"], 1) == 33.3
    # Escalated count: 1 (Case 3)
    assert s["escalated_to_human_count"] == 1
    # DSO > 0
    assert s["dso_days"] > 0.0

    cases = data["cases"]
    assert len(cases) == 3
    inv_nums = [c["invoice_number"] for c in cases]
    assert "INV-2026-001" in inv_nums
    assert "INV-2026-002" in inv_nums
    assert "INV-2026-003" in inv_nums
    # M2's invoice must NOT be here
    assert "INV-M2-001" not in inv_nums


def test_get_receivable_audit_trail(client, seed_receivables):
    """Confirm /api/merchant/receivables/{case_id}/audit-trail returns chronological logs."""
    m1 = seed_receivables["m1"]
    case1 = seed_receivables["case1"]
    app.dependency_overrides[get_current_merchant] = lambda: m1

    res = client.get(f"/api/merchant/receivables/{case1.id}/audit-trail")
    assert res.status_code == 200
    data = res.json()

    assert data["case_id"] == case1.id
    assert data["amount_at_risk"] == 5000.0
    assert data["invoice_number"] == "INV-2026-001"
    assert len(data["logs"]) == 1
    log = data["logs"][0]
    assert log["action_type"] == "send_invoice_reminder"
    assert log["ladder_step"] == 1
    assert log["outcome"] == "sent"
    assert log["guardrail_checks"]["discount_offered"] is False


def test_merchant_isolation_receivables_and_audit_trail(client, seed_receivables):
    """Confirm Merchant 2 cannot see or access Merchant 1's receivables or audit trail."""
    m2 = seed_receivables["m2"]
    case1 = seed_receivables["case1"]  # M1's case
    case4 = seed_receivables["case4"]  # M2's case

    app.dependency_overrides[get_current_merchant] = lambda: m2

    # 1. Listing endpoint for M2 only returns M2's case
    res = client.get("/api/merchant/receivables")
    assert res.status_code == 200
    data = res.json()
    assert data["summary"]["total_cases"] == 1
    assert len(data["cases"]) == 1
    assert data["cases"][0]["invoice_number"] == "INV-M2-001"

    # 2. Accessing M1's case audit trail as M2 returns 404
    res_m1_trail = client.get(f"/api/merchant/receivables/{case1.id}/audit-trail")
    assert res_m1_trail.status_code == 404

    # 3. Accessing M2's own case audit trail succeeds
    res_m2_trail = client.get(f"/api/merchant/receivables/{case4.id}/audit-trail")
    assert res_m2_trail.status_code == 200


def test_advanced_metrics_overdue_receivable_block(client, seed_receivables):
    """Confirm /api/merchant/advanced-metrics populates overdue_receivable with real data."""
    m1 = seed_receivables["m1"]
    app.dependency_overrides[get_current_merchant] = lambda: m1

    res = client.get("/api/merchant/advanced-metrics")
    assert res.status_code == 200
    data = res.json()

    assert "overdue_receivable" in data
    rec = data["overdue_receivable"]

    assert rec["at_risk"] == 17000.0
    assert rec["recovered"] == 3000.0
    assert rec["total_cases"] == 3
    assert rec["recovered_cases"] == 1
    assert rec["escalated"] == 1
    assert rec["dso_days"] > 0.0
