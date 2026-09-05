import pytest
from app.db_models import CheckoutSession, SessionStatus
import uuid
import json

from tests.test_guardrails import client_with_merchant

def test_order_confirmation_page_renders_successfully(client_with_merchant):
    client, db, merchant, customer = client_with_merchant
    
    event_id = "chk_" + uuid.uuid4().hex
    
    # Create a product so the lookup works
    from app.db_models import Product
    p = Product(name="Test Product", price=50.0, stock=10, merchant_id=merchant.id)
    db.add(p)
    db.commit()
    
    cart_items = [{"product_id": p.id, "quantity": 2}]
    
    session = CheckoutSession(
        event_id=event_id, 
        customer_user_id=customer.id, 
        customer_name="Buyer", 
        customer_email="buyer@test.com",
        customer_phone="+919876543210",
        cart_value=100.0, 
        status=SessionStatus.COMPLETED,
        cart_json=json.dumps(cart_items)
    )
    db.add(session)
    db.commit()
    
    response = client.get(f"/order-confirmation?event_id={event_id}")
    assert response.status_code == 200
    assert "Payment Successful!" in response.text
    assert "Test Product" in response.text

def test_manual_call_does_not_500(client_with_merchant):
    client, db, merchant, customer = client_with_merchant
    
    event_id = "chk_" + uuid.uuid4().hex
    
    session = CheckoutSession(
        event_id=event_id, 
        customer_user_id=customer.id, 
        customer_name="Buyer", 
        customer_email="buyer@test.com",
        customer_phone="+919876543210",
        cart_value=100.0, 
        status=SessionStatus.ABANDONED,
    )
    db.add(session)
    db.commit()
    
    # Send a request to trigger a manual call
    response = client.post("/api/merchant/manual-recovery", json={
        "session_id": session.id,
        "channel": "call"
    })
    
    # Either success or fail, but NOT a 500 server error
    assert response.status_code == 200
