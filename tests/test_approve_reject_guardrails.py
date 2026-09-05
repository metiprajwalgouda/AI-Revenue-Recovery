"""
Adversarial tests for POST /api/merchant/recovery-actions/{log_id}/approve
                 and POST /api/merchant/recovery-actions/{log_id}/reject

Each test spins up an isolated in-memory SQLite DB and a TestClient so the
full FastAPI request/response cycle is exercised without touching the real DB
or sending real notifications.

Tests mandated by the project handoff review:
  - Approving an already-approved log fails (400)
  - Approving a log for a different merchant's case fails (403)
  - Rejecting an already-approved log fails (400)
  - Rejecting a log for a different merchant's case fails (403)
Plus baseline happy-path coverage of both endpoints.
"""

import os
import json
import pytest
from unittest.mock import patch

os.environ.setdefault("RAZORPAY_KEY_ID", "test_key")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_secret")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db_models import (
    Base, MerchantUser, CustomerUser, CheckoutSession, SessionStatus,
    RecoveryCase, RecoveryActionLog,
    RecoveryScenario, CaseStatus,
)
from sqlalchemy.pool import StaticPool
from app.db import get_db
from app.main import app


# ---------------------------------------------------------------------------
# DB fixture — isolated in-memory SQLite per test
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
    app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_customer(db, email="c@test.com") -> CustomerUser:
    c = CustomerUser(email=email, password_hash="x")
    db.add(c)
    db.flush()
    return c


def _make_session(db, customer: CustomerUser, merchant_id: int) -> CheckoutSession:
    s = CheckoutSession(
        event_id=f"chk_{customer.id}_{merchant_id}",
        customer_user_id=customer.id,
        customer_email=customer.email,
        customer_name="Test Customer",
        customer_phone="+919999999999",
        cart_value=1000.0,
        status=SessionStatus.ABANDONED,
        cart_json=json.dumps([]),
    )
    db.add(s)
    db.flush()
    return s


def _make_case(db, merchant_id: int, session_id: int) -> RecoveryCase:
    case = RecoveryCase(
        merchant_id=merchant_id,
        checkout_session_id=session_id,
        scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
        amount_at_risk=1000.0,
        status=CaseStatus.AT_RISK,
        ladder_step=1,
    )
    db.add(case)
    db.flush()
    return case


def _make_log(db, case: RecoveryCase, outcome="pending_approval",
              coupon_code=None, amount_offered=None,
              requires_human_approval=True) -> RecoveryActionLog:
    log = RecoveryActionLog(
        case_id=case.id,
        idempotency_key=f"{case.id}:1:test_action",
        ladder_step=1,
        action_type="discount_pending_approval",
        reason="Test action",
        guardrail_checks=json.dumps({}),
        outcome=outcome,
        amount_offered=amount_offered,
        coupon_code=coupon_code,
        requires_human_approval=requires_human_approval,
    )
    db.add(log)
    db.flush()
    return log


# ---------------------------------------------------------------------------
# Guard 1: double-approve rejected with 400
# ---------------------------------------------------------------------------

class TestDoubleApproval:
    def test_approve_already_approved_log_returns_400(self, client, db_session):
        client.post("/api/merchant/signup", json={
            "email": "owner@test.com", "password": "password123", "store_name": "Store"})
        merchant = db_session.query(MerchantUser).filter_by(email="owner@test.com").first()
        assert merchant is not None
        customer = _make_customer(db_session)
        session = _make_session(db_session, customer, merchant.id)
        case = _make_case(db_session, merchant.id, session.id)
        log = _make_log(db_session, case, outcome="approved")
        db_session.commit()

        with patch("app.merchant_extensions.send_recovery_email", return_value=True):
            resp = client.post(f"/api/merchant/recovery-actions/{log.id}/approve")
        assert resp.status_code == 400, resp.text
        assert "pending_approval" in resp.json().get("detail", "").lower() \
               or "double" in resp.json().get("detail", "").lower() \
               or "approved" in resp.json().get("detail", "").lower()

    def test_approve_twice_second_call_returns_400(self, client, db_session):
        client.post("/api/merchant/signup", json={
            "email": "owner2@test.com", "password": "password123", "store_name": "Store"})
        merchant = db_session.query(MerchantUser).filter_by(email="owner2@test.com").first()
        assert merchant is not None
        customer = _make_customer(db_session, "c2@test.com")
        session = _make_session(db_session, customer, merchant.id)
        case = _make_case(db_session, merchant.id, session.id)
        log = _make_log(db_session, case, amount_offered=1000.0)
        db_session.commit()

        with patch("app.merchant_extensions.send_recovery_email", return_value=True), \
             patch("app.agent.payment_failure_actions.send_recovery_email", return_value=True), \
             patch("app.notification_service.send_in_app_notification"):
            r1 = client.post(f"/api/merchant/recovery-actions/{log.id}/approve")
            r2 = client.post(f"/api/merchant/recovery-actions/{log.id}/approve")
        assert r1.status_code == 200, r1.text
        assert r2.status_code == 400, r2.text


# ---------------------------------------------------------------------------
# Guard 4: wrong-merchant returns 403
# ---------------------------------------------------------------------------

class TestMerchantOwnership:
    def test_approve_other_merchants_log_returns_403(self, client, db_session):
        client.post("/api/merchant/signup", json={
            "email": "mA@test.com", "password": "passwordA123", "store_name": "Store A"})
        merchant_a = db_session.query(MerchantUser).filter_by(email="mA@test.com").first()
        assert merchant_a is not None
        customer = _make_customer(db_session, "shared@test.com")
        session = _make_session(db_session, customer, merchant_a.id)
        case_a = _make_case(db_session, merchant_a.id, session.id)
        log_a = _make_log(db_session, case_a)
        db_session.commit()

        # Log out and log in as Merchant B — the attacker
        client.post("/api/merchant/logout")
        client.post("/api/merchant/signup", json={
            "email": "mB@test.com", "password": "passwordB123", "store_name": "Store B"})
        
        with patch("app.merchant_extensions.send_recovery_email", return_value=True):
            resp = client.post(f"/api/merchant/recovery-actions/{log_a.id}/approve")
        assert resp.status_code == 403, resp.text

    def test_reject_other_merchants_log_returns_403(self, client, db_session):
        client.post("/api/merchant/signup", json={
            "email": "mA2@test.com", "password": "passwordA123", "store_name": "Store A"})
        merchant_a = db_session.query(MerchantUser).filter_by(email="mA2@test.com").first()
        assert merchant_a is not None
        customer = _make_customer(db_session, "cx2@test.com")
        session = _make_session(db_session, customer, merchant_a.id)
        case_a = _make_case(db_session, merchant_a.id, session.id)
        log_a = _make_log(db_session, case_a)
        db_session.commit()

        # Log out and log in as Merchant B — the attacker
        client.post("/api/merchant/logout")
        client.post("/api/merchant/signup", json={
            "email": "mB2@test.com", "password": "passwordB123", "store_name": "Store B"})

        resp = client.post(f"/api/merchant/recovery-actions/{log_a.id}/reject")
        assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Guard 1 on /reject: non-pending log returns 400
# ---------------------------------------------------------------------------

class TestRejectGuards:
    def test_reject_already_approved_log_returns_400(self, client, db_session):
        client.post("/api/merchant/signup", json={
            "email": "rej1@test.com", "password": "password123", "store_name": "Store"})
        merchant = db_session.query(MerchantUser).filter_by(email="rej1@test.com").first()
        assert merchant is not None
        customer = _make_customer(db_session, "rc1@test.com")
        session = _make_session(db_session, customer, merchant.id)
        case = _make_case(db_session, merchant.id, session.id)
        log = _make_log(db_session, case, outcome="approved")
        db_session.commit()

        resp = client.post(f"/api/merchant/recovery-actions/{log.id}/reject")
        assert resp.status_code == 400, resp.text

    def test_reject_already_rejected_log_returns_400(self, client, db_session):
        client.post("/api/merchant/signup", json={
            "email": "rej2@test.com", "password": "password123", "store_name": "Store"})
        merchant = db_session.query(MerchantUser).filter_by(email="rej2@test.com").first()
        assert merchant is not None
        customer = _make_customer(db_session, "rc2@test.com")
        session = _make_session(db_session, customer, merchant.id)
        case = _make_case(db_session, merchant.id, session.id)
        log = _make_log(db_session, case, outcome="rejected")
        db_session.commit()

        resp = client.post(f"/api/merchant/recovery-actions/{log.id}/reject")
        assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# 404 guard
# ---------------------------------------------------------------------------

class TestNotFound:
    def test_approve_nonexistent_log_returns_404(self, client, db_session):
        client.post("/api/merchant/signup", json={
            "email": "nf@test.com", "password": "password123", "store_name": "Store"})
        resp = client.post("/api/merchant/recovery-actions/99999/approve")
        assert resp.status_code == 404, resp.text

    def test_reject_nonexistent_log_returns_404(self, client, db_session):
        client.post("/api/merchant/signup", json={
            "email": "nf2@test.com", "password": "password123", "store_name": "Store"})
        resp = client.post("/api/merchant/recovery-actions/99999/reject")
        assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Happy path: approve and reject set correct fields
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_approve_sets_outcome_approved_by_and_approved_at(self, client, db_session):
        client.post("/api/merchant/signup", json={
            "email": "happy@test.com", "password": "password123", "store_name": "Store"})
        merchant = db_session.query(MerchantUser).filter_by(email="happy@test.com").first()
        assert merchant is not None
        customer = _make_customer(db_session, "hc@test.com")
        session = _make_session(db_session, customer, merchant.id)
        case = _make_case(db_session, merchant.id, session.id)
        log = _make_log(db_session, case, amount_offered=1000.0)
        db_session.commit()

        with patch("app.merchant_extensions.send_recovery_email", return_value=True), \
             patch("app.agent.payment_failure_actions.send_recovery_email", return_value=True), \
             patch("app.notification_service.send_in_app_notification"):
            resp = client.post(f"/api/merchant/recovery-actions/{log.id}/approve")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "approved"
        assert body["approved_by"] == merchant.id
        assert body["approved_at"] is not None
        db_session.expire(log)
        assert log.outcome == "approved"
        assert log.approved_by == merchant.id
        assert log.approved_at is not None

    def test_reject_sets_outcome_rejected_and_records_actor(self, client, db_session):
        client.post("/api/merchant/signup", json={
            "email": "rej_hp@test.com", "password": "password123", "store_name": "Store"})
        merchant = db_session.query(MerchantUser).filter_by(email="rej_hp@test.com").first()
        assert merchant is not None
        customer = _make_customer(db_session, "rhc@test.com")
        session = _make_session(db_session, customer, merchant.id)
        case = _make_case(db_session, merchant.id, session.id)
        log = _make_log(db_session, case)
        db_session.commit()

        resp = client.post(f"/api/merchant/recovery-actions/{log.id}/reject")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["outcome"] == "rejected"
        assert body["rejected_by"] == merchant.id
        db_session.expire(log)
        assert log.outcome == "rejected"
        assert log.approved_by == merchant.id
        assert log.approved_at is not None


# ---------------------------------------------------------------------------
# Coupon-aware notification routing
# ---------------------------------------------------------------------------

class TestCouponAwareNotification:
    def test_approve_with_coupon_sends_coupon_in_email_context(self, client, db_session):
        """Log with coupon_code → email context must include coupon_code='SAVE20'."""
        client.post("/api/merchant/signup", json={
            "email": "coupon_m@test.com", "password": "password123", "store_name": "Store"})
        merchant = db_session.query(MerchantUser).filter_by(email="coupon_m@test.com").first()
        assert merchant is not None
        customer = _make_customer(db_session, "cc@test.com")
        session = _make_session(db_session, customer, merchant.id)
        case = _make_case(db_session, merchant.id, session.id)
        log = _make_log(db_session, case, coupon_code="SAVE20", amount_offered=800.0)
        db_session.commit()

        captured = []

        def fake_email(to, subject, template, context):
            captured.append(context)
            return True

        with patch("app.merchant_extensions.send_recovery_email", side_effect=fake_email), \
             patch("app.notification_service.send_in_app_notification"):
            resp = client.post(f"/api/merchant/recovery-actions/{log.id}/approve")

        assert resp.status_code == 200, resp.text
        assert len(captured) == 1, "send_recovery_email not called"
        ctx = captured[0]
        assert ctx.get("coupon_code") == "SAVE20", f"coupon_code missing: {ctx}"
        assert ctx.get("discount_amount") == 200.0, f"discount_amount wrong: {ctx}"
        assert ctx.get("total") == 800.0, f"total wrong: {ctx}"

    def test_approve_without_coupon_uses_no_discount_path(self, client, db_session):
        """Log with no coupon and amount_offered==cart_value → no-discount helper used."""
        client.post("/api/merchant/signup", json={
            "email": "nodisc@test.com", "password": "password123", "store_name": "Store"})
        merchant = db_session.query(MerchantUser).filter_by(email="nodisc@test.com").first()
        assert merchant is not None
        customer = _make_customer(db_session, "ndc@test.com")
        session = _make_session(db_session, customer, merchant.id)
        case = _make_case(db_session, merchant.id, session.id)
        log = _make_log(db_session, case, coupon_code=None, amount_offered=1000.0)
        db_session.commit()

        pf_called = []

        def fake_pf(session, case, link_url, merchant, db):
            pf_called.append(True)

        with patch("app.agent.payment_failure_actions._send_payment_link_notification",
                   side_effect=fake_pf), \
             patch("app.merchant_extensions.send_recovery_email", return_value=True):
            resp = client.post(f"/api/merchant/recovery-actions/{log.id}/approve")

        assert resp.status_code == 200, resp.text
        assert len(pf_called) == 1, "_send_payment_link_notification should have been called"
