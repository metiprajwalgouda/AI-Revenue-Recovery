import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db_models import (
    Base, MerchantUser, CustomerUser, RecoveryCase, RecoveryScenario, CaseStatus
)
from app.db import get_db
from app.main import app
from app.merchant_auth_routes import get_current_merchant
from app.auth import create_merchant_session_token, MERCHANT_COOKIE_NAME


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


@pytest.fixture()
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_coupon_recovery_usage_filters(client, db_session):
    # Setup merchant and customer
    merchant = MerchantUser(email="coupon_merch@test.com", password_hash="hash", store_name="Coupon Store")
    customer = CustomerUser(email="coupon_cust@test.com", password_hash="hash", name="Coupon Buyer", phone="+919876543210")
    db_session.add_all([merchant, customer])
    db_session.commit()
    db_session.refresh(merchant)
    db_session.refresh(customer)

    now = datetime.now(timezone.utc)

    # Recovered case with coupon
    rc1 = RecoveryCase(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        scenario=RecoveryScenario.PAYMENT_FAILURE,
        amount_at_risk=2000.0,
        amount_recovered=1800.0,
        discount_amount=200.0,
        coupon_code_used="SAVE200",
        status=CaseStatus.RECOVERED,
        last_action_at=now - timedelta(days=1),
    )
    # Recovered case with different coupon
    rc2 = RecoveryCase(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        scenario=RecoveryScenario.CHECKOUT_ABANDONMENT,
        amount_at_risk=1000.0,
        amount_recovered=900.0,
        discount_amount=100.0,
        coupon_code_used="WELCOME10",
        status=CaseStatus.RECOVERED,
        last_action_at=now - timedelta(days=5),
    )
    # Intervening case (unrecovered)
    rc3 = RecoveryCase(
        merchant_id=merchant.id,
        customer_user_id=customer.id,
        scenario=RecoveryScenario.OVERDUE_RECEIVABLE,
        amount_at_risk=5000.0,
        amount_recovered=0.0,
        discount_amount=0.0,
        coupon_code_used="DUNNING5",
        status=CaseStatus.INTERVENING,
        last_action_at=now,
    )
    db_session.add_all([rc1, rc2, rc3])
    db_session.commit()

    token = create_merchant_session_token(merchant.id)
    client.cookies.set(MERCHANT_COOKIE_NAME, token)

    # 1. Default (status=recovered)
    res = client.get("/api/merchant/coupon-recovery-usage")
    assert res.status_code == 200
    data = res.json()
    assert data["summary"]["total_coupon_recovered_cases"] == 2
    assert data["summary"]["total_discount_amount"] == 300.0
    assert data["summary"]["total_recovered_amount"] == 2700.0

    # 2. Scenario filter
    res_pf = client.get("/api/merchant/coupon-recovery-usage?scenario=payment_failure")
    assert res_pf.status_code == 200
    assert res_pf.json()["summary"]["total_coupon_recovered_cases"] == 1
    assert res_pf.json()["cases"][0]["coupon_code"] == "SAVE200"

    # 3. Coupon code filter
    res_code = client.get("/api/merchant/coupon-recovery-usage?coupon_code=WELCOME")
    assert res_code.status_code == 200
    assert res_code.json()["summary"]["total_coupon_recovered_cases"] == 1
    assert res_code.json()["cases"][0]["coupon_code"] == "WELCOME10"

    # 4. Status filter: All
    res_all = client.get("/api/merchant/coupon-recovery-usage?status=all")
    assert res_all.status_code == 200
    assert res_all.json()["summary"]["total_coupon_recovered_cases"] == 3

    # 5. Date filter
    start_iso = (now - timedelta(days=2)).isoformat()
    res_date = client.get(f"/api/merchant/coupon-recovery-usage?start_date={start_iso}")
    assert res_date.status_code == 200
    assert res_date.json()["summary"]["total_coupon_recovered_cases"] == 1
    assert res_date.json()["cases"][0]["coupon_code"] == "SAVE200"
