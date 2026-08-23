"""
Tests for decide_action (pure, no side effects) and execute_action (mocked Razorpay).
"""

import os
import pytest
from datetime import datetime
from unittest.mock import MagicMock

os.environ.setdefault("RAZORPAY_KEY_ID", "test_key")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_secret")

from app.models import CheckoutEvent, ClassificationResult, AbandonmentReason, PaymentMethod, RecoveryAction
from app.agent.recovery_actions import decide_action, execute_action, DISCOUNT_BY_REASON
from app.guardrails import MAX_RECOVERY_ATTEMPTS, MAX_DISCOUNT_PERCENT
from app.razorpay_client import RazorpayRecoveryClient, PaymentLinkResult


def make_event(**overrides) -> CheckoutEvent:
    defaults = dict(
        event_id="evt_test", customer_id="cust_test", customer_email="test@example.com",
        customer_phone="+919999999999", cart_value=1500.0,
        payment_method_attempted=PaymentMethod.UPI,
        checkout_started_at=datetime(2026, 1, 1, 10, 0, 0),
        abandoned_at=datetime(2026, 1, 1, 10, 5, 0),
        true_reason=AbandonmentReason.UNKNOWN, payment_status_code=None,
        page_load_time_ms=800, otp_requested=False, otp_verified=False,
        time_on_checkout_page_sec=60, notes=None,
        opted_out_of_marketing=False, previous_recovery_attempts=0,
    )
    defaults.update(overrides)
    return CheckoutEvent(**defaults)


def make_classification(reason=AbandonmentReason.OTP_TIMEOUT, confidence=0.9, method="rule") -> ClassificationResult:
    return ClassificationResult(
        event_id="evt_test", predicted_reason=reason, confidence=confidence,
        method_used=method, reasoning="test reasoning",
    )


# ---------- decide_action tests ----------

def test_guardrail_opt_out_overrides_classification():
    event = make_event(opted_out_of_marketing=True)
    classification = make_classification(reason=AbandonmentReason.CARD_DECLINED, confidence=0.99)
    action = decide_action(event, classification)
    assert action == RecoveryAction.NO_ACTION_RESPECT_OPT_OUT


def test_guardrail_max_retries_overrides_classification():
    event = make_event(previous_recovery_attempts=MAX_RECOVERY_ATTEMPTS)
    classification = make_classification(confidence=0.99)
    action = decide_action(event, classification)
    assert action == RecoveryAction.NO_ACTION_MAX_RETRIES_REACHED


def test_low_confidence_flags_for_manual_review_even_if_rule_based():
    event = make_event()
    classification = make_classification(reason=AbandonmentReason.CARD_DECLINED, confidence=0.3)
    action = decide_action(event, classification)
    assert action == RecoveryAction.FLAG_FOR_MANUAL_REVIEW


def test_card_declined_maps_to_alternate_payment_method():
    event = make_event()
    classification = make_classification(reason=AbandonmentReason.CARD_DECLINED, confidence=0.9)
    assert decide_action(event, classification) == RecoveryAction.OFFER_ALTERNATE_PAYMENT_METHOD


def test_high_amount_hesitation_maps_to_discount_nudge():
    event = make_event()
    classification = make_classification(reason=AbandonmentReason.HIGH_AMOUNT_HESITATION, confidence=0.8)
    assert decide_action(event, classification) == RecoveryAction.SEND_DISCOUNT_NUDGE


# ---------- execute_action tests ----------

@pytest.fixture
def mock_razorpay():
    client = MagicMock(spec=RazorpayRecoveryClient)
    return client


def test_no_action_outcomes_never_call_razorpay(mock_razorpay):
    event = make_event(opted_out_of_marketing=True)
    classification = make_classification()
    outcome = execute_action(event, classification, RecoveryAction.NO_ACTION_RESPECT_OPT_OUT, mock_razorpay)
    assert outcome.action_success is True
    assert outcome.amount_offered is None
    mock_razorpay.create_recovery_payment_link.assert_not_called()


def test_send_payment_link_calls_razorpay_with_full_cart_value(mock_razorpay):
    mock_razorpay.create_recovery_payment_link.return_value = PaymentLinkResult(
        success=True, payment_link_id="plink_1", short_url="https://rzp.io/i/1"
    )
    event = make_event(cart_value=1999.0)
    classification = make_classification(reason=AbandonmentReason.OTP_TIMEOUT, confidence=0.9)
    outcome = execute_action(event, classification, RecoveryAction.SEND_PAYMENT_LINK, mock_razorpay)

    assert outcome.action_success is True
    assert outcome.amount_offered == 1999.0
    call_kwargs = mock_razorpay.create_recovery_payment_link.call_args.kwargs
    assert call_kwargs["amount_rupees"] == 1999.0


def test_discount_nudge_applies_and_caps_discount(mock_razorpay):
    """PRICE_SHOCK_AT_CHECKOUT requests a 20% discount in DISCOUNT_BY_REASON,
    but MAX_DISCOUNT_PERCENT caps it at 15%. This test proves the cap actually bites."""
    mock_razorpay.create_recovery_payment_link.return_value = PaymentLinkResult(
        success=True, payment_link_id="plink_2", short_url="https://rzp.io/i/2"
    )
    event = make_event(cart_value=1000.0)
    classification = make_classification(reason=AbandonmentReason.PRICE_SHOCK_AT_CHECKOUT, confidence=0.8)
    outcome = execute_action(event, classification, RecoveryAction.SEND_DISCOUNT_NUDGE, mock_razorpay)

    requested_discount = DISCOUNT_BY_REASON[AbandonmentReason.PRICE_SHOCK_AT_CHECKOUT]
    assert requested_discount > MAX_DISCOUNT_PERCENT, "test assumes the requested discount exceeds the cap"

    expected_amount = round(1000.0 * (1 - MAX_DISCOUNT_PERCENT / 100), 2)  # capped at 15%, NOT 20%
    assert outcome.amount_offered == expected_amount


def test_amount_offered_not_set_on_razorpay_failure(mock_razorpay):
    """Critical honesty check: if link creation FAILS, amount_offered must be None,
    never a phantom number for a link that doesn't exist."""
    mock_razorpay.create_recovery_payment_link.return_value = PaymentLinkResult(
        success=False, error_message="bad request", error_type="bad_request"
    )
    event = make_event()
    classification = make_classification(reason=AbandonmentReason.OTP_TIMEOUT, confidence=0.9)
    outcome = execute_action(event, classification, RecoveryAction.SEND_PAYMENT_LINK, mock_razorpay)

    assert outcome.action_success is False
    assert outcome.amount_offered is None


def test_confirmed_recovered_amount_never_set_by_execute_action(mock_razorpay):
    """Locks in the offered-vs-confirmed distinction: execute_action (link creation)
    must NEVER set confirmed_recovered_amount -- that requires a separate status check
    against Razorpay confirming the customer actually paid. If this test ever fails,
    someone has reintroduced the 'link created = money recovered' bug."""
    mock_razorpay.create_recovery_payment_link.return_value = PaymentLinkResult(
        success=True, payment_link_id="plink_3", short_url="https://rzp.io/i/3"
    )
    event = make_event()
    classification = make_classification(reason=AbandonmentReason.OTP_TIMEOUT, confidence=0.9)
    outcome = execute_action(event, classification, RecoveryAction.SEND_PAYMENT_LINK, mock_razorpay)

    assert outcome.confirmed_recovered_amount is None


def test_reminder_sms_is_marked_simulated_not_silently_faked(mock_razorpay):
    event = make_event()
    classification = make_classification(reason=AbandonmentReason.ACCIDENTAL_CLOSE, confidence=0.7)
    outcome = execute_action(event, classification, RecoveryAction.SEND_REMINDER_SMS, mock_razorpay)
    assert outcome.action_success is True
    assert "SIMULATED" in outcome.error_message
    mock_razorpay.create_recovery_payment_link.assert_not_called()