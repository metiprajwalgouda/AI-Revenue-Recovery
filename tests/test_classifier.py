"""
Tests for the hybrid classifier.

Rule-based cases: fully deterministic, tested with plain assertions.
LLM cases: one test uses a mock (fast, no API cost, runs in CI),
one test (marked) makes a REAL call to verify the actual integration works
end-to-end -- this is what you'd run manually, not on every CI push.
"""

import os
import pytest
from datetime import datetime
from unittest.mock import MagicMock
from dotenv import load_dotenv

load_dotenv()

os.environ.setdefault("ANTHROPIC_API_KEY", "test_key_placeholder")

from app.models import CheckoutEvent, AbandonmentReason, PaymentMethod
from app.agent.classifier import rule_based_classify, llm_classify, classify


def make_event(**overrides) -> CheckoutEvent:
    """Helper to build a CheckoutEvent with sensible defaults, override what you need per test."""
    defaults = dict(
        event_id="evt_test",
        customer_id="cust_test",
        customer_email="test@example.com",
        customer_phone="+919999999999",
        cart_value=1500.0,
        payment_method_attempted=PaymentMethod.UPI,
        checkout_started_at=datetime(2026, 1, 1, 10, 0, 0),
        abandoned_at=datetime(2026, 1, 1, 10, 5, 0),
        true_reason=AbandonmentReason.UNKNOWN,
        payment_status_code=None,
        page_load_time_ms=800,
        otp_requested=False,
        otp_verified=False,
        time_on_checkout_page_sec=60,
        notes=None,
    )
    defaults.update(overrides)
    return CheckoutEvent(**defaults)


# ---------- Rule-based classification tests ----------

def test_card_declined_detected_by_rule():
    event = make_event(payment_status_code="insufficient_funds")
    result = rule_based_classify(event)
    assert result is not None
    assert result.predicted_reason == AbandonmentReason.CARD_DECLINED
    assert result.method_used == "rule"
    assert result.confidence > 0.9


def test_otp_timeout_detected_by_rule():
    event = make_event(otp_requested=True, otp_verified=False)
    result = rule_based_classify(event)
    assert result.predicted_reason == AbandonmentReason.OTP_TIMEOUT


def test_page_load_slow_detected_by_rule():
    event = make_event(page_load_time_ms=9000)
    result = rule_based_classify(event)
    assert result.predicted_reason == AbandonmentReason.PAGE_LOAD_SLOW


def test_network_drop_detected_by_rule():
    event = make_event(payment_status_code="gateway_timeout")
    result = rule_based_classify(event)
    assert result.predicted_reason == AbandonmentReason.NETWORK_DROP


def test_high_amount_hesitation_detected_by_rule():
    event = make_event(cart_value=35000, time_on_checkout_page_sec=300)
    result = rule_based_classify(event)
    assert result.predicted_reason == AbandonmentReason.HIGH_AMOUNT_HESITATION


def test_accidental_close_detected_by_rule():
    event = make_event(time_on_checkout_page_sec=2, payment_status_code=None, otp_requested=False)
    result = rule_based_classify(event)
    assert result.predicted_reason == AbandonmentReason.ACCIDENTAL_CLOSE


def test_ambiguous_event_returns_none_defers_to_llm():
    """This is the important negative case: signals genuinely don't match any rule,
    so rule_based_classify must return None rather than force a guess."""
    event = make_event(
        payment_status_code=None,
        otp_requested=False,
        page_load_time_ms=900,
        cart_value=1200,
        time_on_checkout_page_sec=90,
        notes="Customer chat: 'let me check with my wife'",
    )
    result = rule_based_classify(event)
    assert result is None


def test_missing_time_on_page_not_confused_with_short_duration():
    """Regression test for a real bug found during dataset validation:
    (value or 0) <= 5 was treating MISSING data (None) the same as a genuinely
    short 0-second visit, wrongly firing the accidental_close rule. Missing
    data must defer to the LLM instead of being silently misclassified."""
    event = make_event(
        time_on_checkout_page_sec=None,
        page_load_time_ms=None,
        payment_status_code=None,
        otp_requested=False,
    )
    result = rule_based_classify(event)
    assert result is None, "Missing time_on_checkout_page_sec must NOT trigger accidental_close"


# ---------- LLM classification tests (mocked) ----------

def test_llm_classify_with_mocked_response():
    event = make_event(notes="let me check with my wife and get back")

    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text='{"predicted_reason": "unknown", "confidence": 0.4, "reasoning": "Customer indicated they need to consult someone else."}')]
    mock_client.messages.create.return_value = mock_response

    result = llm_classify(event, client=mock_client)
    assert result.predicted_reason == AbandonmentReason.UNKNOWN
    assert result.method_used == "llm"
    assert 0 <= result.confidence <= 1


def test_llm_classify_handles_malformed_json_response():
    """Edge case: LLM returns non-JSON garbage -- must fail safe, not crash."""
    event = make_event()
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="I think this is probably a network issue")]
    mock_client.messages.create.return_value = mock_response

    result = llm_classify(event, client=mock_client)
    assert result.predicted_reason == AbandonmentReason.UNKNOWN
    assert result.confidence == 0.0


def test_llm_classify_handles_markdown_wrapped_json():
    """Edge case: LLM wraps valid JSON in markdown fences despite instructions not to."""
    event = make_event()
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text='```json\n{"predicted_reason": "otp_timeout", "confidence": 0.6, "reasoning": "test"}\n```')]
    mock_client.messages.create.return_value = mock_response

    result = llm_classify(event, client=mock_client)
    assert result.predicted_reason == AbandonmentReason.OTP_TIMEOUT


def test_llm_classify_handles_api_exception():
    """Edge case: API call itself fails (timeout, rate limit, auth) -- must fail safe."""
    event = make_event()
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = ConnectionError("API unreachable")

    result = llm_classify(event, client=mock_client)
    assert result.predicted_reason == AbandonmentReason.UNKNOWN
    assert result.confidence == 0.0


# ---------- Full hybrid pipeline test ----------

def test_classify_uses_rule_when_available_skips_llm_entirely():
    """Important: when a rule matches, the LLM must NOT be called at all (saves cost)."""
    event = make_event(payment_status_code="insufficient_funds")
    mock_client = MagicMock()
    result = classify(event, llm_client=mock_client)
    assert result.method_used == "rule"
    mock_client.messages.create.assert_not_called()


def test_classify_falls_back_to_llm_when_no_rule_matches():
    event = make_event(notes="ambiguous case", payment_status_code=None, otp_requested=False,
                        page_load_time_ms=900, cart_value=1200, time_on_checkout_page_sec=90)
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text='{"predicted_reason": "unknown", "confidence": 0.3, "reasoning": "no clear signal"}')]
    mock_client.messages.create.return_value = mock_response

    result = classify(event, llm_client=mock_client)
    assert result.method_used == "llm"
    mock_client.messages.create.assert_called_once()


# ---------- REAL API integration test (run manually, needs real key) ----------

@pytest.mark.skipif(
    os.getenv("ANTHROPIC_API_KEY") in (None, "test_key_placeholder"),
    reason="Requires a real ANTHROPIC_API_KEY to test actual LLM integration",
)
def test_real_llm_call_on_ambiguous_event():
    """This test hits the REAL Claude API. Run manually with a real key set:
    ANTHROPIC_API_KEY=sk-real-key pytest tests/test_classifier.py -v -k real_llm
    """
    event = make_event(
        notes="Customer support chat: 'need to check with my wife before buying this'",
        payment_status_code=None,
        otp_requested=False,
        cart_value=15000,
        time_on_checkout_page_sec=240,
    )
    result = llm_classify(event)
    assert result.method_used == "llm"
    assert result.predicted_reason in list(AbandonmentReason)
    print(f"\nReal LLM classification: {result.predicted_reason.value} "
          f"(confidence={result.confidence}) -- {result.reasoning}")