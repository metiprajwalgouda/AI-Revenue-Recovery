import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.main import app
from app.db_models import Base, MerchantUser, CheckoutSession, SessionStatus, RecoveryOutcomeRecord, Coupon, CustomerUser
from app.merchant_auth_routes import get_current_merchant
from app.db import get_db

@pytest.fixture
def client_with_merchant():
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    TestingSessionLocal = sessionmaker(bind=test_engine)
    db = TestingSessionLocal()

    merchant = MerchantUser(email="test@guard.com", password_hash="hash", store_name="Guard Store", max_discount_pct=20)
    customer = CustomerUser(email="cust@guard.com", password_hash="hash", name="Cust", phone="+919000000000")
    db.add_all([merchant, customer])
    db.commit()
    db.refresh(merchant)
    db.refresh(customer)

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_merchant] = lambda: merchant

    client = TestClient(app)
    yield client, db, merchant, customer
    app.dependency_overrides.clear()
    db.close()

def test_coupon_mutual_exclusivity(client_with_merchant):
    client, db, merchant, customer = client_with_merchant
    
    # Test POST with both -> should 400
    res = client.post("/api/merchant/coupons", json={
        "code": "TESTBOTH",
        "discount_pct": 10,
        "discount_amount": 50,
        "active": True
    })
    assert res.status_code == 400
    assert "Cannot specify both" in res.json()['detail']
    
    # Test POST with neither -> should 400
    res2 = client.post("/api/merchant/coupons", json={
        "code": "TESTNEITHER",
        "active": True
    })
    assert res2.status_code == 400
    assert "Must specify either" in res2.json()['detail']
    
import uuid

def test_manual_recovery_guardrail(client_with_merchant):
    client, db, merchant, customer = client_with_merchant
    
    # Create tech failure session
    ev1 = "chk_tech_" + uuid.uuid4().hex
    s1 = CheckoutSession(event_id=ev1, customer_user_id=customer.id, customer_name="Tech", customer_email="t@t.com", customer_phone="123", cart_value=100.0, status=SessionStatus.ABANDONED)
    db.add(s1)
    db.commit()
    r1 = RecoveryOutcomeRecord(session_id=s1.id, predicted_reason="card_declined", confidence=1.0, classification_method="rule", reasoning="test", action_taken="no_action", action_success=True)
    db.add(r1)
    db.commit()
    
    # Send custom discount in payload
    res = client.post("/api/merchant/manual-recovery", json={
        "session_id": s1.id,
        "channel": "email",
        "discount_pct": 20
    })
    # Should strip discount
    db.refresh(r1)
    assert r1.amount_offered == 100.0  # Cart value, no discount
    
    # Create hesitation session
    ev2 = "chk_hesitate_" + uuid.uuid4().hex
    s2 = CheckoutSession(event_id=ev2, customer_user_id=customer.id, customer_name="Hesitate", customer_email="h@h.com", customer_phone="123", cart_value=100.0, status=SessionStatus.ABANDONED)
    db.add(s2)
    db.commit()
    r2 = RecoveryOutcomeRecord(session_id=s2.id, predicted_reason="price_too_high", confidence=1.0, classification_method="rule", reasoning="test", action_taken="no_action", action_success=True)
    db.add(r2)
    db.commit()
    
    # Send custom discount in payload
    res2 = client.post("/api/merchant/manual-recovery", json={
        "session_id": s2.id,
        "channel": "email",
        "discount_pct": 20
    })
    db.refresh(r2)
    assert r2.amount_offered == 80.0  # 20% discount applied

def test_automated_actions_feed(client_with_merchant):
    client, db, merchant, customer = client_with_merchant
    
    # Create an automated action
    ev3 = "chk_auto_" + uuid.uuid4().hex
    s = CheckoutSession(event_id=ev3, customer_user_id=customer.id, customer_name="Auto", customer_email="a@a.com", customer_phone="123", cart_value=150.0, status=SessionStatus.ABANDONED)
    db.add(s)
    db.commit()
    r = RecoveryOutcomeRecord(session_id=s.id, predicted_reason="price_too_high", confidence=1.0, classification_method="rule", reasoning="test", action_taken="automated_email", amount_offered=135.0, action_success=True)
    db.add(r)
    db.commit()
    
    res = client.get("/api/merchant/automated-actions")
    assert res.status_code == 200
    actions = res.json()
    assert len(actions) > 0
    # Find ours
    my_action = next((a for a in actions if a['customer_name'] == 'Auto'), None)
    assert my_action is not None
    assert my_action['cart_value'] == 150.0
    assert my_action['action_taken'] == 'automated_email'
    assert my_action['amount_offered'] == 135.0
