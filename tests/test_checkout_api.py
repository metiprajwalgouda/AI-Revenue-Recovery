"""
Integration tests for the checkout flow (/api/checkout/*).

Uses the same isolated-test-db pattern as test_main_api.py, plus overrides
get_razorpay_client with a mock so these tests never hit the real Razorpay API.
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

from app.db_models import Base
import app.db as db_module
import app.main as main
from app.razorpay_client import OrderResult


@pytest.fixture(autouse=True)
def isolated_test_db(tmp_path):
    test_db_path = tmp_path / "test_storefront.db"
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
    return MagicMock()


@pytest.fixture
def client(mock_razorpay):
    main.app.dependency_overrides[main.get_razorpay_client] = lambda: mock_razorpay
    yield TestClient(main.app)
    main.app.dependency_overrides.pop(main.get_razorpay_client, None)


def create_test_product(client, price=999.0, stock=10):
    response = client.post("/api/products", json={"name": "Test Product", "price": price, "stock": stock})
    return response.json()["id"]


def test_start_checkout_creates_session_and_order(client, mock_razorpay):
    mock_razorpay.create_order.return_value = OrderResult(success=True, order_id="order_123")
    mock_razorpay.key_id = "rzp_test_fake"
    product_id = create_test_product(client, price=500.0, stock=5)

    response = client.post("/api/checkout/start", json={
        "customer_email": "buyer@example.com",
        "customer_phone": "+919999999999",
        "cart_items": [{"product_id": product_id, "quantity": 2}],
    })
    assert response.status_code == 200
    body = response.json()
    assert body["order_id"] == "order_123"
    assert body["cart_value"] == 1000.0
    assert body["event_id"].startswith("chk_")


def test_start_checkout_rejects_nonexistent_product(client, mock_razorpay):
    response = client.post("/api/checkout/start", json={
        "customer_email": "buyer@example.com",
        "customer_phone": "+919999999999",
        "cart_items": [{"product_id": 99999, "quantity": 1}],
    })
    assert response.status_code == 400
    mock_razorpay.create_order.assert_not_called()


def test_start_checkout_rejects_insufficient_stock(client, mock_razorpay):
    product_id = create_test_product(client, price=100.0, stock=2)
    response = client.post("/api/checkout/start", json={
        "customer_email": "buyer@example.com",
        "customer_phone": "+919999999999",
        "cart_items": [{"product_id": product_id, "quantity": 5}],
    })
    assert response.status_code == 400
    mock_razorpay.create_order.assert_not_called()


def test_start_checkout_uses_server_price_not_client_supplied(client, mock_razorpay):
    """Security-relevant: cart_value is ALWAYS recomputed server-side from the
    database price. Our request schema doesn't even accept a client price/total --
    this test documents and locks in that design choice."""
    mock_razorpay.create_order.return_value = OrderResult(success=True, order_id="order_x")
    mock_razorpay.key_id = "rzp_test_fake"
    product_id = create_test_product(client, price=2000.0, stock=10)

    response = client.post("/api/checkout/start", json={
        "customer_email": "buyer@example.com",
        "customer_phone": "+919999999999",
        "cart_items": [{"product_id": product_id, "quantity": 1}],
    })
    assert response.json()["cart_value"] == 2000.0
    call_kwargs = mock_razorpay.create_order.call_args.kwargs
    assert call_kwargs["amount_rupees"] == 2000.0


def test_start_checkout_fails_cleanly_if_order_creation_fails(client, mock_razorpay):
    mock_razorpay.create_order.return_value = OrderResult(success=False, error_message="Razorpay down")
    product_id = create_test_product(client)

    response = client.post("/api/checkout/start", json={
        "customer_email": "buyer@example.com",
        "customer_phone": "+919999999999",
        "cart_items": [{"product_id": product_id, "quantity": 1}],
    })
    assert response.status_code == 502


def test_complete_checkout_with_valid_signature_marks_completed(client, mock_razorpay):
    mock_razorpay.create_order.return_value = OrderResult(success=True, order_id="order_1")
    mock_razorpay.key_id = "rzp_test_fake"
    mock_razorpay.verify_payment_signature.return_value = True
    product_id = create_test_product(client)

    start = client.post("/api/checkout/start", json={
        "customer_email": "buyer@example.com", "customer_phone": "+919999999999",
        "cart_items": [{"product_id": product_id, "quantity": 1}],
    }).json()

    complete = client.post("/api/checkout/complete", json={
        "event_id": start["event_id"],
        "razorpay_order_id": "order_1",
        "razorpay_payment_id": "pay_1",
        "razorpay_signature": "valid_sig",
    })
    assert complete.status_code == 200
    assert complete.json()["status"] == "completed"


def test_complete_checkout_rejects_invalid_signature(client, mock_razorpay):
    """CRITICAL security test: a forged signature must NEVER mark a checkout as paid."""
    mock_razorpay.create_order.return_value = OrderResult(success=True, order_id="order_1")
    mock_razorpay.key_id = "rzp_test_fake"
    mock_razorpay.verify_payment_signature.return_value = False
    product_id = create_test_product(client)

    start = client.post("/api/checkout/start", json={
        "customer_email": "buyer@example.com", "customer_phone": "+919999999999",
        "cart_items": [{"product_id": product_id, "quantity": 1}],
    }).json()

    complete = client.post("/api/checkout/complete", json={
        "event_id": start["event_id"],
        "razorpay_order_id": "order_1",
        "razorpay_payment_id": "pay_1",
        "razorpay_signature": "FORGED",
    })
    assert complete.status_code == 400


def test_complete_checkout_unknown_event_id_returns_404(client, mock_razorpay):
    response = client.post("/api/checkout/complete", json={
        "event_id": "chk_does_not_exist",
        "razorpay_order_id": "order_1", "razorpay_payment_id": "pay_1", "razorpay_signature": "sig",
    })
    assert response.status_code == 404


def test_abandon_checkout_marks_session_abandoned(client, mock_razorpay):
    mock_razorpay.create_order.return_value = OrderResult(success=True, order_id="order_1")
    mock_razorpay.key_id = "rzp_test_fake"
    product_id = create_test_product(client)

    start = client.post("/api/checkout/start", json={
        "customer_email": "buyer@example.com", "customer_phone": "+919999999999",
        "cart_items": [{"product_id": product_id, "quantity": 1}],
    }).json()

    response = client.post("/api/checkout/abandon", json={
        "event_id": start["event_id"], "reason_hint": "user_closed_popup",
    })
    assert response.status_code == 200
    assert response.json()["status"] == "abandoned"


def test_abandon_after_completion_does_not_downgrade_status(client, mock_razorpay):
    """Race condition test: success + dismiss callbacks can both fire close together
    in the real widget. Abandon must NOT overwrite a completed order back to abandoned."""
    mock_razorpay.create_order.return_value = OrderResult(success=True, order_id="order_1")
    mock_razorpay.key_id = "rzp_test_fake"
    mock_razorpay.verify_payment_signature.return_value = True
    product_id = create_test_product(client)

    start = client.post("/api/checkout/start", json={
        "customer_email": "buyer@example.com", "customer_phone": "+919999999999",
        "cart_items": [{"product_id": product_id, "quantity": 1}],
    }).json()

    client.post("/api/checkout/complete", json={
        "event_id": start["event_id"], "razorpay_order_id": "order_1",
        "razorpay_payment_id": "pay_1", "razorpay_signature": "valid_sig",
    })

    abandon_response = client.post("/api/checkout/abandon", json={"event_id": start["event_id"]})
    assert abandon_response.json()["status"] == "already_completed"


def test_abandon_unknown_event_id_returns_404(client, mock_razorpay):
    response = client.post("/api/checkout/abandon", json={"event_id": "chk_ghost"})
    assert response.status_code == 404

    
# ---------- Recovery pipeline wiring (abandon triggers classify -> decide -> execute) ----------

from app.razorpay_client import PaymentLinkResult


@pytest.fixture
def mock_recovery_razorpay():
    mock = MagicMock()
    mock.create_recovery_payment_link.return_value = PaymentLinkResult(
        success=True, payment_link_id="plink_recovery_1", short_url="https://rzp.io/i/recovery1"
    )
    return mock


@pytest.fixture
def client_with_recovery(mock_razorpay, mock_recovery_razorpay):
    main.app.dependency_overrides[main.get_razorpay_client] = lambda: mock_razorpay
    main.app.dependency_overrides[main.get_recovery_razorpay_client] = lambda: mock_recovery_razorpay
    main.app.dependency_overrides[main.get_llm_client] = lambda: MagicMock()
    yield TestClient(main.app)
    main.app.dependency_overrides.pop(main.get_razorpay_client, None)
    main.app.dependency_overrides.pop(main.get_recovery_razorpay_client, None)
    main.app.dependency_overrides.pop(main.get_llm_client, None)


def test_abandon_triggers_recovery_and_returns_outcome(client_with_recovery, mock_razorpay, mock_recovery_razorpay):
    mock_razorpay.create_order.return_value = OrderResult(success=True, order_id="order_1")
    mock_razorpay.key_id = "rzp_test_fake"
    product_id = create_test_product(client_with_recovery)

    start = client_with_recovery.post("/api/checkout/start", json={
        "customer_email": "buyer@example.com", "customer_phone": "+919999999999",
        "cart_items": [{"product_id": product_id, "quantity": 1}],
    }).json()

    abandon = client_with_recovery.post("/api/checkout/abandon", json={"event_id": start["event_id"]})
    assert abandon.status_code == 200
    body = abandon.json()
    assert body["status"] == "abandoned"
    assert body["recovery"] is not None
    assert body["recovery"]["action_taken"] is not None
    assert body["recovery"]["classification_method"] in ("rule", "llm")


def test_abandon_called_twice_does_not_duplicate_recovery_attempt(client_with_recovery, mock_razorpay, mock_recovery_razorpay):
    """Idempotency guard: calling /abandon twice for the same session (e.g. a retried
    request, or 'ondismiss' firing twice) must NOT create a second Razorpay recovery
    attempt or a duplicate RecoveryOutcomeRecord."""
    mock_razorpay.create_order.return_value = OrderResult(success=True, order_id="order_2")
    mock_razorpay.key_id = "rzp_test_fake"
    product_id = create_test_product(client_with_recovery)

    start = client_with_recovery.post("/api/checkout/start", json={
        "customer_email": "buyer@example.com", "customer_phone": "+919999999999",
        "cart_items": [{"product_id": product_id, "quantity": 1}],
    }).json()

    first = client_with_recovery.post("/api/checkout/abandon", json={"event_id": start["event_id"]})
    second = client_with_recovery.post("/api/checkout/abandon", json={"event_id": start["event_id"]})

    assert first.json()["recovery"]["action_taken"] == second.json()["recovery"]["action_taken"]
    assert mock_recovery_razorpay.create_recovery_payment_link.call_count <= 1


def test_abandon_uses_recovery_client_not_checkout_client(client_with_recovery, mock_razorpay, mock_recovery_razorpay):
    """The checkout-order client (get_razorpay_client) and the recovery client
    (get_recovery_razorpay_client) are DELIBERATELY separate dependencies -- this
    test locks in that recovery actions never accidentally call the order-creation client."""
    mock_razorpay.create_order.return_value = OrderResult(success=True, order_id="order_3")
    mock_razorpay.key_id = "rzp_test_fake"
    product_id = create_test_product(client_with_recovery)

    start = client_with_recovery.post("/api/checkout/start", json={
        "customer_email": "buyer@example.com", "customer_phone": "+919999999999",
        "cart_items": [{"product_id": product_id, "quantity": 1}],
    }).json()

    client_with_recovery.post("/api/checkout/abandon", json={"event_id": start["event_id"]})

    mock_razorpay.create_recovery_payment_link.assert_not_called()


def test_completed_session_never_triggers_recovery(client_with_recovery, mock_razorpay, mock_recovery_razorpay):
    """A completed order must never trigger a recovery attempt, even if /abandon
    is called on it afterward (race condition case)."""
    mock_razorpay.create_order.return_value = OrderResult(success=True, order_id="order_4")
    mock_razorpay.key_id = "rzp_test_fake"
    mock_razorpay.verify_payment_signature.return_value = True
    product_id = create_test_product(client_with_recovery)

    start = client_with_recovery.post("/api/checkout/start", json={
        "customer_email": "buyer@example.com", "customer_phone": "+919999999999",
        "cart_items": [{"product_id": product_id, "quantity": 1}],
    }).json()

    client_with_recovery.post("/api/checkout/complete", json={
        "event_id": start["event_id"], "razorpay_order_id": "order_4",
        "razorpay_payment_id": "pay_4", "razorpay_signature": "sig_4",
    })

    abandon = client_with_recovery.post("/api/checkout/abandon", json={"event_id": start["event_id"]})
    assert abandon.json()["status"] == "already_completed"
    assert abandon.json()["recovery"] is None
    mock_recovery_razorpay.create_recovery_payment_link.assert_not_called()