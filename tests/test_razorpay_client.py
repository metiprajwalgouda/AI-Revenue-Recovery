"""
Tests for RazorpayRecoveryClient.

We mock the underlying razorpay.Client so these tests:
1. Run instantly, no network needed
2. Don't burn real API calls / rate limits
3. Can simulate errors that are hard to trigger on-demand with the real API
   (server errors, bad requests) -- this is how we test edge cases deterministically
"""

import os
import sys
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch
from razorpay.errors import BadRequestError, ServerError

os.environ.setdefault("RAZORPAY_KEY_ID", "test_key_id")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_key_secret")

# Ensure the project-root module is discoverable when pytest is launched from
# the tests directory or by an editor's language server.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.razorpay_client import RazorpayRecoveryClient, PaymentLinkResult


@pytest.fixture
def client():
    return RazorpayRecoveryClient(key_id="test_key_id", key_secret="test_key_secret")


def test_missing_keys_raises_error(monkeypatch):
    # Must clear env vars too, not just pass None as args -- the constructor
    # falls back to os.getenv() as a convenience, so a leftover env var from
    # another test/session would silently mask this failure otherwise.
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    with pytest.raises(ValueError):
        RazorpayRecoveryClient(key_id=None, key_secret=None)


def test_zero_amount_rejected_without_calling_api(client):
    """Edge case: cart_value = 0 from our synthetic dataset should never reach Razorpay."""
    result = client.create_recovery_payment_link(
        amount_rupees=0,
        customer_name="Test User",
        customer_email="test@example.com",
        customer_phone="+919999999999",
        description="test",
        reference_id="evt_edge_zero",
    )
    assert result.success is False
    assert result.error_type == "bad_request"


def test_negative_amount_rejected(client):
    result = client.create_recovery_payment_link(
        amount_rupees=-500,
        customer_name="Test User",
        customer_email="test@example.com",
        customer_phone="+919999999999",
        description="test",
        reference_id="evt_edge_negative",
    )
    assert result.success is False


def test_successful_payment_link_creation(client):
    fake_response = {"id": "plink_abc123", "short_url": "https://rzp.io/i/abc123"}
    with patch.object(client.client.payment_link, "create", return_value=fake_response):
        result = client.create_recovery_payment_link(
            amount_rupees=1499,
            customer_name="Test User",
            customer_email="test@example.com",
            customer_phone="+919999999999",
            description="Recover abandoned cart",
            reference_id="evt_123",
        )
    assert result.success is True
    assert result.payment_link_id == "plink_abc123"
    assert result.short_url.startswith("https://")


def test_amount_converted_to_paise_correctly(client):
    """Razorpay expects paise, not rupees -- this is a classic real-world bug source."""
    captured_payload = {}

    def fake_create(payload):
        captured_payload.update(payload)
        return {"id": "plink_test", "short_url": "https://rzp.io/i/test"}

    with patch.object(client.client.payment_link, "create", side_effect=fake_create):
        client.create_recovery_payment_link(
            amount_rupees=299.50,
            customer_name="Test User",
            customer_email="test@example.com",
            customer_phone="+919999999999",
            description="test",
            reference_id="evt_paise_check",
        )
    assert captured_payload["amount"] == 29950  # 299.50 rupees -> 29950 paise


def test_bad_request_error_handled_gracefully(client):
    """Edge case: malformed data (e.g. bad phone format) should not crash the batch."""
    with patch.object(
        client.client.payment_link,
        "create",
        side_effect=BadRequestError("Invalid contact number"),
    ):
        result = client.create_recovery_payment_link(
            amount_rupees=500,
            customer_name="Test User",
            customer_email="bad-email",
            customer_phone="123",  # malformed
            description="test",
            reference_id="evt_malformed",
        )
    assert result.success is False
    assert result.error_type == "bad_request"


def test_server_error_handled_gracefully(client):
    """Edge case: Razorpay's servers are down/erroring -- our batch must not crash."""
    with patch.object(
        client.client.payment_link, "create", side_effect=ServerError("Internal error")
    ):
        result = client.create_recovery_payment_link(
            amount_rupees=500,
            customer_name="Test User",
            customer_email="test@example.com",
            customer_phone="+919999999999",
            description="test",
            reference_id="evt_server_error",
        )
    assert result.success is False
    assert result.error_type == "server_error"


def test_unexpected_exception_does_not_crash(client):
    """Catch-all: even a totally unexpected exception type must return a clean failure,
    never propagate and kill the whole batch run."""
    with patch.object(
        client.client.payment_link, "create", side_effect=ConnectionError("network unreachable")
    ):
        result = client.create_recovery_payment_link(
            amount_rupees=500,
            customer_name="Test User",
            customer_email="test@example.com",
            customer_phone="+919999999999",
            description="test",
            reference_id="evt_network_drop",
        )
    assert result.success is False
    assert result.error_type == "unknown"