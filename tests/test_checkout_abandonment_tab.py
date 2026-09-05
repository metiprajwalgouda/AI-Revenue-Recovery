"""
Tests for Checkout Abandonment tab, API endpoints, and metrics integration.

Coverage:
  1. GET /merchant/checkout-abandonment: HTML page rendering and authentication guard.
  2. GET /api/merchant/checkout-abandonment: Summary KPIs (At Risk, Recovered, Lost, Rate %, Escalated, Circuit Breakers)
     and cases table payload with classification badges.
  3. GET /api/merchant/checkout-abandonment/{case_id}/audit-trail: Chronological logs with guardrail breakdown.
  4. Strict merchant scoping: Merchant isolation on list and audit trail.
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
def seed_abandonment_data(db_session):
    now = datetime.now(timezone.utc)

    # Merchant 1
    m1 = MerchantUser(
        email="merchant_ab1@example.com",
        password_hash="hash1",
        store_name="Abandonment Store 1",
    )
    # Merchant 2
    m2 = MerchantUser(
        email="merchant_ab2@example.com",
        password_hash="hash2",
        store_name="Abandonment Store 2",
    )
    # Customer
    c1 = CustomerUser(
        email="alice@example.com",
        password_hash="hashc",
        name="Alice Shopper",
        phone="+919876543210",
    )
    c2 = CustomerUser(
        email="charlie@example.com",
        password_hash="hashc2",
        name="Charlie Checkout",
        phone="+919876543212",
    )
    db_session.add_all([m1, m2, c1, c2])
    db_session.flush()

    # Session 1 (M1)
    s1 = CheckoutSession(
        event_id="chk_sess_001",
        customer_user_id=c1.id,
        customer_name=c1.name,
        customer_email=c1.email,
        customer_phone=c1.phone,
        cart_value=4500.0,
        status=SessionStatus.ABANDONED,
    )
    db_session.add(s1)
    db_session.flush()

    # Case 1 (M1): Active checkout abandonment (Price Sensitivity)
    case1 = RecoveryCase(
        merchant_id=m1.id,
        customer_user_id=c1.id,
        checkout_session_id=s1.id,
        scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
        amount_at_risk=4500.0,
        amount_recovered=0.0,
        classification="price_sensitivity",
        status=CaseStatus.INTERVENING,
        ladder_step=1,
        last_action_at=now - timedelta(hours=2),
    )
    db_session.add(case1)
    db_session.flush()

    log1 = RecoveryActionLog(
        case_id=case1.id,
        idempotency_key=f"{case1.id}:1:send_discount_email",
        ladder_step=1,
        action_type="email",
        reason="Customer abandoned cart due to price sensitivity — sent 10% discount offer.",
        guardrail_checks=json.dumps({"ladder_step": 1, "contact_made": True, "discount_offered": True, "max_discount_pct": 10}),
        outcome="sent",
        amount_offered=450.0,
        coupon_code="SAVE10",
    )
    db_session.add(log1)

    # Session 2 (M1)
    s2 = CheckoutSession(
        event_id="chk_sess_002",
        customer_user_id=c2.id,
        customer_name=c2.name,
        customer_email=c2.email,
        customer_phone=c2.phone,
        cart_value=2500.0,
        status=SessionStatus.RECOVERED,
    )
    db_session.add(s2)
    db_session.flush()

    # Case 2 (M1): Recovered checkout abandonment
    case2 = RecoveryCase(
        merchant_id=m1.id,
        customer_user_id=c2.id,
        checkout_session_id=s2.id,
        scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
        amount_at_risk=2500.0,
        amount_recovered=2500.0,
        classification="technical_glitch",
        status=CaseStatus.RECOVERED,
        ladder_step=1,
        last_action_at=now - timedelta(days=1),
    )
    db_session.add(case2)

    # Case 3 (M1): Lost / Circuit breaker trip (contact touches = 3)
    case3 = RecoveryCase(
        merchant_id=m1.id,
        customer_user_id=c1.id,
        scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
        amount_at_risk=8000.0,
        amount_recovered=0.0,
        classification="trust_concerns",
        status=CaseStatus.LOST,
        contact_touches=3,
        ladder_step=3,
        last_action_at=now - timedelta(days=3),
    )
    db_session.add(case3)

    # Case 4 (M1): Escalated case
    case4 = RecoveryCase(
        merchant_id=m1.id,
        customer_user_id=c2.id,
        scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
        amount_at_risk=15000.0,
        amount_recovered=0.0,
        classification="high_value_abandonment",
        status=CaseStatus.ESCALATED,
        escalated_to_human=True,
        escalation_reason="high_cart_value_vip",
        ladder_step=2,
        last_action_at=now - timedelta(hours=5),
    )
    db_session.add(case4)

    # Case 5 (M2): Belongs to Merchant 2 (Sentinel)
    case5 = RecoveryCase(
        merchant_id=m2.id,
        customer_user_id=c1.id,
        scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
        amount_at_risk=99999.0,
        amount_recovered=0.0,
        classification="price_sensitivity",
        status=CaseStatus.INTERVENING,
        ladder_step=1,
    )
    db_session.add(case5)
    db_session.flush()

    log5 = RecoveryActionLog(
        case_id=case5.id,
        idempotency_key=f"{case5.id}:1:m2_log",
        ladder_step=1,
        action_type="email",
        reason="M2 isolated action",
        outcome="sent",
    )
    db_session.add(log5)

    db_session.commit()
    return {"m1": m1, "m2": m2, "c1": c1, "c2": c2, "case1": case1, "case2": case2, "case3": case3, "case4": case4, "case5": case5}


def test_merchant_checkout_abandonment_html_page_auth(client, seed_abandonment_data):
    """Confirm /merchant/checkout-abandonment page returns 200 when authenticated and redirects when not."""
    from app.auth import create_merchant_session_token, MERCHANT_COOKIE_NAME
    m1 = seed_abandonment_data["m1"]

    # Unauthenticated -> redirect to /merchant/login
    res_unauth = client.get("/merchant/checkout-abandonment", follow_redirects=False)
    assert res_unauth.status_code in (302, 307)
    assert "/merchant/login" in res_unauth.headers.get("location", "")

    # Authenticated via session cookie
    token = create_merchant_session_token(m1.id)
    client.cookies.set(MERCHANT_COOKIE_NAME, token)
    res_auth = client.get("/merchant/checkout-abandonment")
    assert res_auth.status_code == 200
    assert "Checkout Abandonment" in res_auth.text
    assert "sidebar-link" in res_auth.text
    assert "checkout-abandonment" in res_auth.text


def test_get_merchant_checkout_abandonment_kpi_and_cases(client, seed_abandonment_data):
    """Confirm /api/merchant/checkout-abandonment computes accurate 6 KPIs and returns case list."""
    m1 = seed_abandonment_data["m1"]
    app.dependency_overrides[get_current_merchant] = lambda: m1

    res = client.get("/api/merchant/checkout-abandonment")
    assert res.status_code == 200
    data = res.json()

    assert "summary" in data
    assert "cases" in data

    s = data["summary"]
    # M1 has 4 cases:
    # Case 1: 4500 (intervening -> open)
    # Case 2: 2500 recovered
    # Case 3: 8000 (lost)
    # Case 4: 15000 (escalated -> open)
    # Open at risk: 4500 + 15000 = 19500
    assert s["total_at_risk"] == 19500.0
    assert s["total_recovered"] == 2500.0
    assert s["total_lost"] == 8000.0
    assert s["total_cases"] == 4
    assert s["recovered_cases"] == 1
    assert round(s["recovery_rate_pct"], 1) == 25.0
    assert s["escalated_to_human_count"] == 1
    assert s["circuit_breaker_trips"] == 1

    cases = data["cases"]
    assert len(cases) == 4
    case_ids = [c["id"] for c in cases]
    assert seed_abandonment_data["case1"].id in case_ids
    assert seed_abandonment_data["case2"].id in case_ids
    assert seed_abandonment_data["case3"].id in case_ids
    assert seed_abandonment_data["case4"].id in case_ids
    # M2's sentinel case must NOT appear
    assert seed_abandonment_data["case5"].id not in case_ids

    # Verify case fields
    c1_row = next(c for c in cases if c["id"] == seed_abandonment_data["case1"].id)
    assert c1_row["amount_at_risk"] == 4500.0
    assert c1_row["classification"] == "price_sensitivity"
    assert c1_row["customer_name"] == "Alice Shopper"
    assert c1_row["customer_email"] == "alice@example.com"
    assert c1_row["ladder_step"] == 1
    assert c1_row["status"] == "intervening"


def test_get_checkout_abandonment_audit_trail(client, seed_abandonment_data):
    """Confirm /api/merchant/checkout-abandonment/{case_id}/audit-trail returns chronological logs."""
    m1 = seed_abandonment_data["m1"]
    case1 = seed_abandonment_data["case1"]
    app.dependency_overrides[get_current_merchant] = lambda: m1

    res = client.get(f"/api/merchant/checkout-abandonment/{case1.id}/audit-trail")
    assert res.status_code == 200
    data = res.json()

    assert data["case_id"] == case1.id
    assert data["amount_at_risk"] == 4500.0
    assert len(data["logs"]) == 1
    log = data["logs"][0]
    assert log["action_type"] == "email"
    assert log["ladder_step"] == 1
    assert log["outcome"] == "sent"
    assert log["amount_offered"] == 450.0
    assert log["coupon_code"] == "SAVE10"
    assert log["guardrail_checks"]["discount_offered"] is True


def test_merchant_isolation_checkout_abandonment_and_audit_trail(client, seed_abandonment_data):
    """Confirm Merchant 2 cannot see or access Merchant 1's abandonment cases or audit trail."""
    m2 = seed_abandonment_data["m2"]
    case1 = seed_abandonment_data["case1"]  # M1's case
    case5 = seed_abandonment_data["case5"]  # M2's case

    app.dependency_overrides[get_current_merchant] = lambda: m2

    # 1. Listing endpoint for M2 only returns M2's case
    res = client.get("/api/merchant/checkout-abandonment")
    assert res.status_code == 200
    data = res.json()
    assert data["summary"]["total_cases"] == 1
    assert len(data["cases"]) == 1
    assert data["cases"][0]["id"] == case5.id
    assert data["cases"][0]["amount_at_risk"] == 99999.0

    # 2. Accessing M1's case audit trail as M2 returns 404
    res_m1_trail = client.get(f"/api/merchant/checkout-abandonment/{case1.id}/audit-trail")
    assert res_m1_trail.status_code == 404

    # 3. Accessing M2's own case audit trail succeeds
    res_m2_trail = client.get(f"/api/merchant/checkout-abandonment/{case5.id}/audit-trail")
    assert res_m2_trail.status_code == 200
