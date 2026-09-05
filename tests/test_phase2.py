import pytest
from app.db_models import CheckoutSession, MerchantUser, CustomerUser, RecoveryOutcomeRecord, SessionStatus
from app.agent.decision_engine import decide_intervention
from app.main import app, get_db
from fastapi.testclient import TestClient

@pytest.fixture
def client_phase2(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db_models import Base

    test_db_path = tmp_path / "test_phase2.db"
    test_engine = create_engine(f"sqlite:///{test_db_path}", connect_args={"check_same_thread": False})
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    Base.metadata.create_all(bind=test_engine)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    c = TestClient(app)
    
    # create merchant
    res = c.post("/api/merchant/signup", json={"email": "m1@example.com", "password": "password123", "store_name": "Store"}); assert res.status_code == 200, res.text
    # log out / clear cookies manually if needed, or rely on distinct cookie names (merchant_session / customer_session)
    
    # create customer
    res = c.post("/api/customer/signup", json={"email": "c1@example.com", "password": "password123", "name": "Cust", "phone": "+919999999999"}); assert res.status_code == 200, res.text
    
    yield c
    app.dependency_overrides.pop(get_db, None)

@pytest.fixture
def test_db():
    db_gen = app.dependency_overrides[get_db]()
    return next(db_gen)

def test_reason_mapping():
    session = CheckoutSession(id=1, cart_value=1000, previous_recovery_attempts=0)
    merchant = MerchantUser(id=1, min_discount_pct=10, max_discount_pct=20, max_recovery_attempts=3)
    customer = CustomerUser(id=1, opted_out_of_marketing=False)
    
    action, reasoning, discount = decide_intervention(session, "high_amount_hesitation", merchant, customer)
    assert action == "send_discount_offer_email"
    assert discount == 10  # min_discount_pct
    assert "Allowed discount due to high_amount_hesitation" in reasoning

    action, reasoning, discount = decide_intervention(session, "bank_failure", merchant, customer)
    assert action == "send_reminder_email"
    assert discount == 0.0

def test_opt_out_and_limits():
    session = CheckoutSession(id=1, cart_value=1000, previous_recovery_attempts=3)
    merchant = MerchantUser(id=1, max_recovery_attempts=3)
    customer = CustomerUser(id=1, opted_out_of_marketing=False)
    
    # Exceeds max attempts
    action, reasoning, discount = decide_intervention(session, "high_amount_hesitation", merchant, customer)
    assert action == "suppress"
    assert "max_attempts_reached" in reasoning

    # Opted out
    session.previous_recovery_attempts = 0
    customer.opted_out_of_marketing = True
    action, reasoning, discount = decide_intervention(session, "high_amount_hesitation", merchant, customer)
    assert action == "suppress"
    assert "opted out" in reasoning.lower()

def test_settings_bounds(client_phase2, test_db):
    payload = {
        "contact_email": "test@test.com",
        "phone": "123",
        "business_category": "retail",
        "min_discount_pct": 30,
        "max_discount_pct": 20,
        "max_recovery_attempts": 3,
        "high_value_threshold_amount": 1000.0
    }
    res = client_phase2.post("/api/merchant/settings", json=payload)
    assert res.status_code == 400
    assert "min_discount_pct cannot be greater than max_discount_pct" in res.text

def test_priority_tagging_and_manual_followup(client_phase2, test_db):
    merchant = test_db.query(MerchantUser).first()
    merchant.high_value_threshold_amount = 3000
    test_db.commit()

    # Create high value session
    customer = test_db.query(CustomerUser).first()
    session = CheckoutSession(event_id="high123", customer_user_id=customer.id, customer_email="test@test", customer_name="t", customer_phone="", cart_value=3500.0, status=SessionStatus.ABANDONED, is_high_priority=True)
    test_db.add(session)
    test_db.commit()
    
    res = client_phase2.post(f"/api/merchant/priority/{session.id}/contact")
    assert res.status_code == 200
    assert res.json()["status"] == "success"
    
    outcome = test_db.query(RecoveryOutcomeRecord).filter_by(session_id=session.id).first()
    assert outcome is not None
    assert outcome.action_taken == "manual_followup"

def test_discounted_resume_flow(client_phase2, test_db, monkeypatch):
    customer = test_db.query(CustomerUser).first()
    
    session = CheckoutSession(event_id="res123", customer_user_id=customer.id, customer_email="c@test", customer_name="c", customer_phone="", cart_value=1000.0, status=SessionStatus.ABANDONED)
    test_db.add(session)
    test_db.commit()
    
    outcome = RecoveryOutcomeRecord(session_id=session.id, amount_offered=900.0, predicted_reason="high_amount_hesitation", confidence=0.9, classification_method="llm", reasoning="reason", action_taken="send_discount_offer_email", action_success=True, delivery_status="sent")
    test_db.add(outcome)
    test_db.commit()
    
    from app.db_models import Product
    prod = Product(id=1, merchant_id=1, name="Test Product", price=1000.0, stock=10, is_active=True)
    test_db.add(prod)
    test_db.commit()
    
    import os
    monkeypatch.setenv("MOCK_PAYMENTS", "true")
    
    res = client_phase2.post("/api/checkout/start", json={
        "cart_items": [{"product_id": 1, "quantity": 1}],
        "resume_event_id": "res123"
    })
    
    assert res.status_code == 200
    data = res.json()
    assert data["cart_value"] == 900.0
    assert data["amount_paise"] == 90000
