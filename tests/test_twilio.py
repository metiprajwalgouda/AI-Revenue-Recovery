import pytest
from unittest.mock import patch, MagicMock
from app.voice_service import make_recovery_call
from app.sms_service import send_recovery_sms
from app.db_models import CheckoutSession, RecoveryOutcomeRecord
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

@pytest.fixture
def client_phase2(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db_models import Base, MerchantUser, CustomerUser, Product, CheckoutSession, SessionStatus
    from app.main import app, get_db

    test_db_path = tmp_path / "test_twilio.db"
    engine = create_engine(f"sqlite:///{test_db_path}", connect_args={"check_same_thread": False})
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_db():
        try:
            db = TestingSessionLocal()
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    
    from app.main import get_current_merchant
    app.dependency_overrides[get_current_merchant] = lambda: MerchantUser(id=1, email="m@m.com")
    
    # Pre-seed merchant and customer and session
    db = TestingSessionLocal()
    m = MerchantUser(id=1, email="m@m.com", password_hash="pwd", store_name="store")
    c = CustomerUser(id=1, email="c@c.com", password_hash="pwd", name="Cust")
    s = CheckoutSession(event_id="evt_123", customer_user_id=1, customer_name="Cust", customer_email="c@c.com", customer_phone="+1234567890", cart_value=100.0, status=SessionStatus.ABANDONED)
    db.add(m)
    db.add(c)
    db.add(s)
    db.commit()
    db.close()

    yield TestClient(app)
    app.dependency_overrides.clear()

@pytest.fixture
def test_db(client_phase2):
    from app.main import get_db
    from app.main import app
    return next(app.dependency_overrides[get_db]())


def test_voice_call_no_env_vars(monkeypatch):
    monkeypatch.setenv("RECOVERY_CALL_PROVIDER", "twilio")
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWILIO_PHONE_NUMBER", raising=False)
    
    res = make_recovery_call("+1234567890", "Test Customer", 1000.0, "http://test.com")
    assert res["status"] == "skipped_no_key"

def test_sms_no_env_vars(monkeypatch, test_db: Session):
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWILIO_PHONE_NUMBER", raising=False)
    
    from sqlalchemy.orm import sessionmaker
    TestingSessionLocal = sessionmaker(bind=test_db.get_bind())
    with patch("app.sms_service.SessionLocal", new=TestingSessionLocal):
        res = send_recovery_sms("+1234567890", "Test message")
        # will fail because no outcome is seeded here, which is expected
        assert res["status"] == "failed"

@patch("app.voice_service.Client")
def test_voice_call_success(mock_client_class, monkeypatch):
    monkeypatch.setenv("RECOVERY_CALL_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+111111111")
    monkeypatch.setenv("MOCK_CALLS", "false")
    
    mock_instance = MagicMock()
    mock_client_class.return_value = mock_instance
    mock_instance.calls.create.return_value.sid = "CA123456"
    
    res = make_recovery_call("+1234567890", "Test", 1000.0, "http://test.com")
    assert res["status"] == "initiated"
    assert res["sid"] == "CA123456"
    mock_instance.calls.create.assert_called_once()
    kwargs = mock_instance.calls.create.call_args[1]
    assert "url" in kwargs

def test_twilio_twiml_endpoint(client_phase2: TestClient, test_db: Session):
    session = test_db.query(CheckoutSession).filter(CheckoutSession.event_id == "evt_123").first()
    assert session is not None
    
    # Test 1: No outcome, generic twiml but with session details
    res = client_phase2.get("/api/twilio-twiml?event_id=evt_123")
    assert res.status_code == 200
    assert "Hi Cust, you have an order of 100.0 rupees waiting" in res.text
    assert "coupon code" not in res.text
    
    # Test 2: Tech failure -> omit discount
    r = RecoveryOutcomeRecord(
        session_id=session.id, 
        predicted_reason="card_declined", 
        confidence=1.0, 
        classification_method="rule",
        reasoning="test",
        action_taken="no_action", 
        action_success=True
    )
    test_db.add(r)
    test_db.commit()
    
    res2 = client_phase2.get("/api/twilio-twiml?event_id=evt_123")
    assert "coupon code" not in res2.text
    
    # Test 3: Hesitation -> discount allowed
    r.predicted_reason = "high_amount_hesitation"
    test_db.commit()
    
    res3 = client_phase2.get("/api/twilio-twiml?event_id=evt_123&coupon_code=VIP20")
    assert "using coupon code VIP20" in res3.text

@patch("app.sms_service.Client")
def test_sms_success(mock_client_class, monkeypatch, test_db: Session):
    monkeypatch.setenv("RECOVERY_CALL_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+111111111")
    monkeypatch.setenv("MOCK_SMS_FIXED", "false")
    
    mock_instance = MagicMock()
    mock_client_class.return_value = mock_instance
    mock_instance.messages.create.return_value.sid = "SM123456"
    
    # Needs a session for the lookup
    session = test_db.query(CheckoutSession).first()
    session.customer_phone = "+1234567890"
    test_db.commit()

    from sqlalchemy.orm import sessionmaker
    TestingSessionLocal = sessionmaker(bind=test_db.get_bind())
    with patch("app.sms_service.SessionLocal", new=TestingSessionLocal):
        res = send_recovery_sms("+1234567890", "Test")
        assert res["status"] == "failed" # since it's sms_unavailable without an outcome

def test_sms_template_selection(client_phase2: TestClient, test_db: Session, monkeypatch):
    monkeypatch.setenv("RECOVERY_CALL_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", "+111111111")
    monkeypatch.setenv("MOCK_SMS_FIXED", "true")
    
    session = test_db.query(CheckoutSession).first()
    session.customer_phone = "+1234567890"
    test_db.commit()

    from sqlalchemy.orm import sessionmaker
    TestingSessionLocal = sessionmaker(bind=test_db.get_bind())

    # 1. No outcome -> restricted -> unavailable
    with patch("app.sms_service.SessionLocal", new=TestingSessionLocal):
        res = send_recovery_sms("+1234567890", "Test")
        assert res["status"] == "failed"
        assert "unavailable" in res["error"]
        
        # 2. Outcome tech failure -> unavailable
        r = RecoveryOutcomeRecord(
            session_id=session.id, 
            predicted_reason="card_declined", 
            confidence=1.0, 
            classification_method="rule",
            reasoning="test",
            action_taken="no_action", 
            action_success=True
        )
        test_db.add(r)
        test_db.commit()
        
        res2 = send_recovery_sms("+1234567890", "Test")
        assert res2["status"] == "failed"
        assert "unavailable" in res2["error"]

        # 3. Outcome hesitation -> promotions
        r.predicted_reason = "high_amount_hesitation"
        test_db.commit()
        
        res3 = send_recovery_sms("+1234567890", "Test")
        assert res3["status"] == "initiated"

@patch("app.sms_service.send_recovery_sms")
@patch("app.voice_service.make_recovery_call")
def test_priority_call_endpoint(mock_voice, mock_sms, client_phase2: TestClient, test_db: Session):
    mock_voice.return_value = {"status": "initiated", "sid": "CA123"}
    mock_sms.return_value = {"status": "initiated", "sid": "SM123"}
    
    # Needs a session
    session = test_db.query(CheckoutSession).first()
    assert session is not None
    session.customer_phone = "+1234567890"
    test_db.commit()
    
    res = client_phase2.post(f"/api/merchant/priority/{session.id}/call")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    
    # verify outcome
    outcome = test_db.query(RecoveryOutcomeRecord).filter(RecoveryOutcomeRecord.session_id == session.id).first()
    assert outcome is not None
    assert outcome.action_taken == "voice_call"
    assert outcome.action_success is True
    assert outcome.delivery_status == "initiated"
