"""
Tests for compute_live_analytics against a real (test) SQLite database with
seeded CheckoutSession + RecoveryOutcomeRecord rows -- checks exact numbers,
not just "it doesn't crash".
"""

import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db_models import Base, CheckoutSession, RecoveryOutcomeRecord, SessionStatus, CustomerUser, MerchantUser, RecoveryCase, CaseStatus, RecoveryScenario
from app.live_analytics import compute_live_analytics, compute_confirmed_discounts


@pytest.fixture
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/test_analytics.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    yield db
    db.close()


def seed_customer_and_merchant(db):
    customer = CustomerUser(email="cust@test.com", password_hash="x")
    merchant = MerchantUser(email="merch@test.com", password_hash="x", store_name="Test")
    db.add_all([customer, merchant])
    db.commit()
    return customer, merchant


def make_session(db, customer, event_id, cart_value, status):
    session = CheckoutSession(
        event_id=event_id, customer_user_id=customer.id,
        customer_email=customer.email, customer_phone="+919999999999",
        cart_value=cart_value, status=status,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def make_outcome(db, session, action_taken, amount_offered=None, confirmed=None, method="rule"):
    outcome = RecoveryOutcomeRecord(
        session_id=session.id, predicted_reason="card_declined", confidence=0.9,
        classification_method=method, action_taken=action_taken, action_success=True,
        amount_offered=amount_offered, confirmed_recovered_amount=confirmed,
    )
    db.add(outcome)
    db.commit()
    return outcome


def test_empty_database_returns_zeros(db_session):
    result = compute_live_analytics(db_session)
    assert result["started_count"] == 0
    assert result["completed_count"] == 0
    assert result["abandoned_count"] == 0
    assert result["total_abandoned_value"] == 0
    assert result["total_confirmed_discounts"] == 0.0
    assert result["recovery_rate_pct"] == 0.0


def test_funnel_counts_by_status(db_session):
    customer, merchant = seed_customer_and_merchant(db_session)
    make_session(db_session, customer, "chk_1", 1000, SessionStatus.STARTED)
    make_session(db_session, customer, "chk_2", 2000, SessionStatus.COMPLETED)
    make_session(db_session, customer, "chk_3", 1500, SessionStatus.ABANDONED)
    make_session(db_session, customer, "chk_4", 500, SessionStatus.ABANDONED)

    result = compute_live_analytics(db_session)
    assert result["started_count"] == 1
    assert result["completed_count"] == 1
    assert result["abandoned_count"] == 2
    assert result["total_abandoned_value"] == 2000  # 1500 + 500


def test_confirmed_discounts_sourced_from_recovered_cases(db_session):
    """Discounts must strictly source from RecoveryCase.discount_amount on recovered cases,
    matching Revenue Intelligence's advanced metrics."""
    customer, merchant = seed_customer_and_merchant(db_session)
    s1 = make_session(db_session, customer, "chk_paid", 1000, SessionStatus.ABANDONED)
    s2 = make_session(db_session, customer, "chk_unpaid", 2000, SessionStatus.ABANDONED)

    # Case 1: Recovered with a 150 discount
    c1 = RecoveryCase(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        checkout_session_id=s1.id,
        scenario=RecoveryScenario.PAYMENT_FAILURE,
        status=CaseStatus.RECOVERED,
        amount_at_risk=1000.0,
        amount_recovered=850.0,
        discount_amount=150.0,
        coupon_code_used="SAVE15",
    )
    # Case 2: Open/Unrecovered case with offered discount but not redeemed
    c2 = RecoveryCase(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        checkout_session_id=s2.id,
        scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
        status=CaseStatus.INTERVENING,
        amount_at_risk=2000.0,
        amount_recovered=0.0,
        discount_amount=300.0,  # offered but not recovered
    )
    db_session.add_all([c1, c2])
    db_session.commit()

    # Shared helper function test
    assert compute_confirmed_discounts(db_session, merchant_id=merchant.id) == 150.0

    # Analytics calculation test
    result = compute_live_analytics(db_session, merchant_id=merchant.id)
    assert result["total_confirmed_discounts"] == 150.0
    assert result["total_discount_cost"] == 150.0
    assert result["total_confirmed_recovered"] == 850.0


def test_action_and_method_breakdown(db_session):
    customer, merchant = seed_customer_and_merchant(db_session)
    s1 = make_session(db_session, customer, "chk_a", 500, SessionStatus.ABANDONED)
    s2 = make_session(db_session, customer, "chk_b", 700, SessionStatus.ABANDONED)
    s3 = make_session(db_session, customer, "chk_c", 900, SessionStatus.ABANDONED)

    make_outcome(db_session, s1, "send_payment_link", method="rule")
    make_outcome(db_session, s2, "send_payment_link", method="llm")
    make_outcome(db_session, s3, "flag_for_manual_review", method="llm")

    result = compute_live_analytics(db_session)
    assert result["action_breakdown"]["send_payment_link"] == 2
    assert result["action_breakdown"]["flag_for_manual_review"] == 1
    assert result["classification_method_breakdown"]["rule"] == 1
    assert result["classification_method_breakdown"]["llm"] == 2


def test_recent_outcomes_ordered_most_recent_first(db_session):
    customer, merchant = seed_customer_and_merchant(db_session)
    s1 = make_session(db_session, customer, "chk_old", 100, SessionStatus.ABANDONED)
    o1 = make_outcome(db_session, s1, "send_payment_link")
    o1.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db_session.commit()

    s2 = make_session(db_session, customer, "chk_new", 200, SessionStatus.ABANDONED)
    o2 = make_outcome(db_session, s2, "send_payment_link")
    o2.created_at = datetime(2026, 1, 2, tzinfo=timezone.utc)
    db_session.commit()

    result = compute_live_analytics(db_session)
    assert result["recent_outcomes"][0]["event_id"] == "chk_new"
    assert result["recent_outcomes"][1]["event_id"] == "chk_old"


def test_recent_outcomes_capped_at_ten(db_session):
    customer, merchant = seed_customer_and_merchant(db_session)
    for i in range(15):
        s = make_session(db_session, customer, f"chk_{i}", 100, SessionStatus.ABANDONED)
        make_outcome(db_session, s, "send_payment_link")

    result = compute_live_analytics(db_session)
    assert len(result["recent_outcomes"]) == 10