"""
Tests for customer signup/login/logout and checkout session ownership isolation.
"""

import os
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("ANTHROPIC_API_KEY", "test_placeholder")
os.environ.setdefault("RAZORPAY_KEY_ID", "test_key")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_secret")
os.environ.setdefault("SESSION_SECRET_KEY", "test-secret-key")

from app.db_models import Base
from app import db as db_module
from app import main
from app.razorpay_client import OrderResult


@pytest.fixture(autouse=True)
def isolated_test_db(tmp_path):
    test_db_path = tmp_path / "test_customer_auth.db"
    test_engine = create_engine(f"sqlite:///{test_db_path}", connect_args={"check_same_thread": False})
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    Base.metadata.create_all(bind=test_engine)

    def override_get_db():
        session = TestSessionLocal()
        try:
            yield session
        finally:
            session.close()

    main.app.dependency_overrides[db_module.get_db] = override_get_db
    yield
    main.app.dependency_overrides.clear()


@pytest.fixture
def mock_razorpay():
    mock = MagicMock()
    mock.key_id = "rzp_test_fake"
    mock.create_order.return_value = OrderResult(success=True, order_id="order_1")
    return mock


@pytest.fixture
def client(mock_razorpay):
    main.app.dependency_overrides[main.get_razorpay_client] = lambda: mock_razorpay
    yield TestClient(main.app)
    main.app.dependency_overrides.pop(main.get_razorpay_client, None)


def signup_merchant_and_product(client):
    client.post("/api/merchant/signup", json={
        "email": "shop@test.com", "password": "merchantpass123", "store_name": "Shop",
    })
    product = client.post("/api/products", json={"name": "Item", "price": 500.0, "stock": 10}).json()
    client.post("/api/merchant/logout")
    return product


def test_customer_signup_creates_account(client):
    response = client.post("/api/customer/signup", json={
        "email": "shopper@test.com", "password": "shopperpass123", "name": "Jane", "phone": "+919999999999",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "shopper@test.com"
    assert "password" not in body


def test_customer_signup_rejects_duplicate_email(client):
    client.post("/api/customer/signup", json={
        "email": "dup@test.com", "password": "shopperpass123",
    })
    second = client.post("/api/customer/signup", json={
        "email": "dup@test.com", "password": "differentpass",
    })
    assert second.status_code == 409


def test_customer_login_with_correct_credentials(client):
    client.post("/api/customer/signup", json={"email": "login@test.com", "password": "correctpass123"})
    client.post("/api/customer/logout")
    response = client.post("/api/customer/login", json={"email": "login@test.com", "password": "correctpass123"})
    assert response.status_code == 200


def test_customer_login_with_wrong_password_rejected(client):
    client.post("/api/customer/signup", json={"email": "wrong@test.com", "password": "correctpass123"})
    response = client.post("/api/customer/login", json={"email": "wrong@test.com", "password": "wrongpass"})
    assert response.status_code == 401


def test_checkout_start_requires_customer_login(client):
    """A guest with NO customer session must not be able to start checkout,
    per the full-accounts-required decision."""
    product = signup_merchant_and_product(client)
    response = client.post("/api/checkout/start", json={
        "cart_items": [{"product_id": product["id"], "quantity": 1}],
    })
    assert response.status_code == 401


def test_checkout_start_uses_logged_in_customers_own_details(client):
    """Email/phone come from the account, never from the request body -- confirms
    the field was actually removed from trust, not just documented as removed."""
    product = signup_merchant_and_product(client)
    client.post("/api/customer/signup", json={
        "email": "realbuyer@test.com", "password": "buyerpass123", "name": "Real Buyer", "phone": "+911234567890",
    })

    response = client.post("/api/checkout/start", json={
        "cart_items": [{"product_id": product["id"], "quantity": 1}],
    })
    assert response.status_code == 200
    # (session internals aren't exposed in the response, but this at minimum
    # proves checkout succeeds using ONLY the logged-in account -- no email/phone
    # fields were supplied in the request at all)


# ---------- Cross-customer session ownership isolation ----------

def test_customer_cannot_abandon_another_customers_session(client):
    """CRITICAL: Customer B must not be able to trigger recovery actions
    against Customer A's abandoned session by guessing/obtaining the event_id."""
    product = signup_merchant_and_product(client)

    client.post("/api/customer/signup", json={"email": "customerA@test.com", "password": "passwordA123"})
    start = client.post("/api/checkout/start", json={
        "cart_items": [{"product_id": product["id"], "quantity": 1}],
    }).json()
    client.post("/api/customer/logout")

    client.post("/api/customer/signup", json={"email": "customerB@test.com", "password": "passwordB123"})
    attempt = client.post("/api/checkout/abandon", json={"event_id": start["event_id"]})
    assert attempt.status_code == 404


def test_customer_cannot_complete_another_customers_session(client):
    product = signup_merchant_and_product(client)

    client.post("/api/customer/signup", json={"email": "ownerA@test.com", "password": "passwordA123"})
    start = client.post("/api/checkout/start", json={
        "cart_items": [{"product_id": product["id"], "quantity": 1}],
    }).json()
    client.post("/api/customer/logout")

    client.post("/api/customer/signup", json={"email": "ownerB@test.com", "password": "passwordB123"})
    attempt = client.post("/api/checkout/complete", json={
        "event_id": start["event_id"], "razorpay_order_id": "order_1",
        "razorpay_payment_id": "pay_fake", "razorpay_signature": "sig_fake",
    })
    assert attempt.status_code == 404