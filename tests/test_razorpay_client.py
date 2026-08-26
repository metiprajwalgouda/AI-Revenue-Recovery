"""
Tests for RazorpayRecoveryClient.

We mock the underlying razorpay.Client so these tests:
1. Run instantly, no network needed
2. Don't burn real API calls / rate limits
3. Can simulate errors that are hard to trigger on-demand with the real API
   (server errors, bad requests) -- this is how we test edge cases deterministically
"""

import os
import time
import pytest
from unittest.mock import MagicMock, patch
from razorpay.errors import BadRequestError, ServerError

os.environ.setdefault("RAZORPAY_KEY_ID", "test_key_id")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_key_secret")

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


# ---------- Discovered from a real pipeline run against live Razorpay test mode ----------

def test_duplicate_reference_id_gets_distinct_error_type_not_generic_bad_request(client):
    """Real bug found running the full 86-event batch: re-running the pipeline against
    Razorpay hits reference_ids that already exist from a PREVIOUS run (Razorpay keeps
    these forever). This must be classified distinctly from a generic bad_request,
    since retrying a duplicate is pointless but retrying other bad requests might not be."""
    with patch.object(
        client.client.payment_link,
        "create",
        side_effect=BadRequestError(
            "payment link with given reference_id: evt_123 already exists. "
            "Please create a payment link with a different reference_id"
        ),
    ):
        result = client.create_recovery_payment_link(
            amount_rupees=500, customer_name="Test User", customer_email="test@example.com",
            customer_phone="+919999999999", description="test", reference_id="evt_123",
        )
    assert result.success is False
    assert result.error_type == "duplicate_reference_id"


def test_rate_limit_retries_with_backoff_then_succeeds(client, monkeypatch):
    """Real bug found running the full batch: firing ~86 requests back-to-back tripped
    Razorpay's test-mode rate limit ('Too many requests'). This must be retried
    (transient), not treated as a permanent failure."""
    monkeypatch.setattr(time, "sleep", lambda _: None)  # don't actually wait in tests

    call_count = {"n": 0}

    def fake_create(payload):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise BadRequestError("Too many requests")
        return {"id": "plink_after_retry", "short_url": "https://rzp.io/i/retry"}

    with patch.object(client.client.payment_link, "create", side_effect=fake_create):
        result = client.create_recovery_payment_link(
            amount_rupees=500, customer_name="Test User", customer_email="test@example.com",
            customer_phone="+919999999999", description="test", reference_id="evt_rate_limited",
        )
    assert result.success is True
    assert result.payment_link_id == "plink_after_retry"
    assert call_count["n"] == 3  # failed twice, succeeded on the 3rd attempt


def test_rate_limit_gives_up_after_max_retries(client, monkeypatch):
    """If rate limiting persists beyond max_retries, fail cleanly with error_type
    'rate_limited' -- distinguishable from a permanent bad_request in the dashboard."""
    monkeypatch.setattr(time, "sleep", lambda _: None)

    with patch.object(
        client.client.payment_link, "create", side_effect=BadRequestError("Too many requests")
    ):
        result = client.create_recovery_payment_link(
            amount_rupees=500, customer_name="Test User", customer_email="test@example.com",
            customer_phone="+919999999999", description="test", reference_id="evt_always_limited",
            max_retries=2,
        )
    assert result.success is False
    assert result.error_type == "rate_limited"


# ---------- SimulatedRazorpayClient tests ----------

from app.razorpay_client import SimulatedRazorpayClient


def test_simulated_client_deterministic_across_calls():
    """Same reference_id must ALWAYS produce the same outcome, regardless of how many
    times we call it or in what order -- this is what makes dashboard numbers reproducible."""
    sim1 = SimulatedRazorpayClient()
    sim2 = SimulatedRazorpayClient()  # fresh instance, should still agree

    result1 = sim1.create_recovery_payment_link(
        amount_rupees=1000, customer_name="A", customer_email="a@x.com",
        customer_phone="+919999999999", description="test", reference_id="evt_fixed_123",
    )
    result2 = sim2.create_recovery_payment_link(
        amount_rupees=1000, customer_name="A", customer_email="a@x.com",
        customer_phone="+919999999999", description="test", reference_id="evt_fixed_123",
    )
    assert result1.success == result2.success
    assert result1.payment_link_id == result2.payment_link_id


def test_simulated_client_zero_amount_still_rejected():
    """Guardrail must hold even in simulation -- simulating shouldn't bypass real invariants."""
    sim = SimulatedRazorpayClient()
    result = sim.create_recovery_payment_link(
        amount_rupees=0, customer_name="A", customer_email="a@x.com",
        customer_phone="+919999999999", description="test", reference_id="evt_zero",
    )
    assert result.success is False


def test_simulated_client_fetch_status_matches_creation_outcome():
    sim = SimulatedRazorpayClient(paid_rate=1.0, failure_rate=0.0)  # force everything to "paid"
    result = sim.create_recovery_payment_link(
        amount_rupees=500, customer_name="A", customer_email="a@x.com",
        customer_phone="+919999999999", description="test", reference_id="evt_always_paid",
    )
    assert result.success is True
    status = sim.fetch_payment_link_status(result.payment_link_id)
    assert status == "paid"


def test_simulated_client_respects_paid_rate_zero():
    sim = SimulatedRazorpayClient(paid_rate=0.0, failure_rate=0.0)  # nothing gets marked paid
    result = sim.create_recovery_payment_link(
        amount_rupees=500, customer_name="A", customer_email="a@x.com",
        customer_phone="+919999999999", description="test", reference_id="evt_never_paid",
    )
    assert result.success is True
    status = sim.fetch_payment_link_status(result.payment_link_id)
    assert status == "created"


def test_simulated_client_status_consistent_across_fresh_instances():
    """Regression test for a real bug found while wiring run_pipeline.py + reconcile_pipeline.py
    as SEPARATE script invocations: status must be derivable from payment_link_id ALONE
    (not reference_id), since a fresh process/instance has no shared memory and
    RecoveryOutcome only persists payment_link_id, not the original reference_id."""
    creation_time_client = SimulatedRazorpayClient(paid_rate=0.35, failure_rate=0.05)
    result = creation_time_client.create_recovery_payment_link(
        amount_rupees=1200, customer_name="A", customer_email="a@x.com",
        customer_phone="+919999999999", description="test", reference_id="evt_persisted_case",
    )
    assert result.success is True
    status_at_creation = creation_time_client.fetch_payment_link_status(result.payment_link_id)

    # Simulate a completely separate process: fresh instance, no shared in-memory state.
    reconciliation_time_client = SimulatedRazorpayClient(paid_rate=0.35, failure_rate=0.05)
    status_at_reconciliation = reconciliation_time_client.fetch_payment_link_status(result.payment_link_id)

    assert status_at_creation == status_at_reconciliation, (
        "Status must be identical whether checked at creation time or later from a "
        "fresh instance -- this is required for run_pipeline.py and reconcile_pipeline.py "
        "to agree with each other as separate script runs."
    )


# ---------- Order creation + signature verification (real storefront checkout flow) ----------

def test_create_order_success(client):
    fake_response = {"id": "order_abc123"}
    with patch.object(client.client.order, "create", return_value=fake_response):
        result = client.create_order(amount_rupees=999.0, receipt="chk_test_001")
    assert result.success is True
    assert result.order_id == "order_abc123"


def test_create_order_zero_amount_rejected_without_calling_api(client):
    with patch.object(client.client.order, "create") as mock_create:
        result = client.create_order(amount_rupees=0, receipt="chk_zero")
    assert result.success is False
    mock_create.assert_not_called()


def test_create_order_converts_to_paise(client):
    captured = {}

    def fake_create(payload):
        captured.update(payload)
        return {"id": "order_test"}

    with patch.object(client.client.order, "create", side_effect=fake_create):
        client.create_order(amount_rupees=1499.50, receipt="chk_paise_test")
    assert captured["amount"] == 149950


def test_create_order_handles_api_failure_gracefully(client):
    with patch.object(client.client.order, "create", side_effect=ServerError("down")):
        result = client.create_order(amount_rupees=500, receipt="chk_fail")
    assert result.success is False
    assert result.order_id is None


def test_verify_payment_signature_success(client):
    with patch.object(client.client.utility, "verify_payment_signature", return_value=None):
        is_valid = client.verify_payment_signature("order_1", "pay_1", "sig_1")
    assert is_valid is True


def test_verify_payment_signature_rejects_forged_signature(client):
    """CRITICAL security test: a forged/incorrect signature must be rejected, not
    accidentally accepted. This is what stops someone from calling /checkout/complete
    directly with made-up IDs to fake a payment."""
    import razorpay as razorpay_module

    with patch.object(
        client.client.utility,
        "verify_payment_signature",
        side_effect=razorpay_module.errors.SignatureVerificationError("bad signature"),
    ):
        is_valid = client.verify_payment_signature("order_1", "pay_1", "forged_signature")
    assert is_valid is False


def test_verify_payment_signature_handles_unexpected_error_safely(client):
    """Edge case: any unexpected error during verification must fail CLOSED (reject),
    never fail open and accept an unverified payment."""
    with patch.object(client.client.utility, "verify_payment_signature", side_effect=ConnectionError("down")):
        is_valid = client.verify_payment_signature("order_1", "pay_1", "sig_1")
    assert is_valid is False