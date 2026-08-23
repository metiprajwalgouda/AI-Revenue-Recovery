"""
Full pipeline integration test: dataset -> guardrails -> classify -> decide ->
execute -> reconcile -> dashboard. Uses a mocked Razorpay client with
DETERMINISTIC, varied outcomes (some paid, some unpaid, some failed) so we
can assert exact expected numbers -- not just "it ran without crashing."
"""

import os
import pytest
from unittest.mock import MagicMock

os.environ.setdefault("RAZORPAY_KEY_ID", "test_key")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_secret")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_key_placeholder")

from datetime import datetime
from app.models import CheckoutEvent, PaymentMethod, AbandonmentReason
from app.agent.classifier import classify
from app.agent.recovery_actions import decide_action, execute_action
from app.reconciliation import reconcile_batch
from app.dashboard import compute_summary
from app.audit_log import AuditLogger
from app.razorpay_client import PaymentLinkResult


def make_event(event_id, cart_value, **overrides) -> CheckoutEvent:
    defaults = dict(
        event_id=event_id, customer_id=f"cust_{event_id}", customer_email=f"{event_id}@example.com",
        customer_phone="+919999999999", cart_value=cart_value,
        payment_method_attempted=PaymentMethod.UPI,
        checkout_started_at=datetime(2026, 1, 1, 10, 0, 0),
        abandoned_at=datetime(2026, 1, 1, 10, 5, 0),
        true_reason=AbandonmentReason.OTP_TIMEOUT,
        payment_status_code=None, page_load_time_ms=800,
        otp_requested=True, otp_verified=False,
        time_on_checkout_page_sec=45, notes=None,
        opted_out_of_marketing=False, previous_recovery_attempts=0,
    )
    defaults.update(overrides)
    return CheckoutEvent(**defaults)


def test_full_pipeline_with_mixed_outcomes():
    events = [
        make_event("evt_paid", 1000.0),                              # link created, gets marked paid
        make_event("evt_unpaid", 2000.0),                             # link created, never paid
        make_event("evt_link_fail", 500.0),                           # link creation itself fails
        make_event("evt_opted_out", 800.0, opted_out_of_marketing=True),  # guardrail blocks, no link at all
    ]

    mock_razorpay = MagicMock()

    def fake_create(reference_id, **kwargs):
        if reference_id == "evt_link_fail":
            return PaymentLinkResult(success=False, error_message="bad request", error_type="bad_request")
        return PaymentLinkResult(success=True, payment_link_id=f"plink_{reference_id}", short_url="https://rzp.io/i/x")

    def fake_fetch_status(payment_link_id):
        if payment_link_id == "plink_evt_paid":
            return "paid"
        if payment_link_id == "plink_evt_unpaid":
            return "created"  # sent but not paid yet
        return None

    mock_razorpay.create_recovery_payment_link.side_effect = fake_create
    mock_razorpay.fetch_payment_link_status.side_effect = fake_fetch_status

    outcomes = []
    original_cart_values = {}
    for event in events:
        original_cart_values[event.event_id] = event.cart_value
        classification = classify(event, llm_client=MagicMock())  # all these events hit rules, LLM unused
        action = decide_action(event, classification)
        outcome = execute_action(event, classification, action, mock_razorpay)
        outcomes.append(outcome)

    reconciled = reconcile_batch(outcomes, mock_razorpay)
    summary = compute_summary(reconciled, original_cart_values)

    # ---- Assertions on exact expected numbers ----
    assert summary["total_events"] == 4
    assert summary["total_abandoned_value"] == 1000.0 + 2000.0 + 500.0 + 800.0  # 4300.0

    # Only evt_paid and evt_unpaid successfully got a link created (evt_link_fail failed,
    # evt_opted_out never attempted one due to the guardrail)
    assert summary["total_amount_offered"] == 1000.0 + 2000.0  # 3000.0

    # Only evt_paid actually shows status="paid" -- this is the ONLY honest recovered number
    assert summary["total_confirmed_recovered"] == 1000.0

    assert summary["guardrail_blocked_count"] == 1  # evt_opted_out correctly counted as a guardrail block
    assert summary["exceptions"] == [
        {"event_id": "evt_link_fail", "action": "send_payment_link", "error": "bad request"}
    ]

    # Recovery rate should reflect confirmed money against TOTAL abandoned value, not offered
    expected_rate = round(1000.0 / 4300.0 * 100, 2)
    assert summary["recovery_rate_of_total_abandoned_pct"] == expected_rate


def test_audit_log_survives_and_reloads_all_outcomes(tmp_path):
    log_path = tmp_path / "audit_test.jsonl"
    logger = AuditLogger(log_path=str(log_path))

    event = make_event("evt_audit_1", 1200.0)
    classification = classify(event, llm_client=MagicMock())
    mock_razorpay = MagicMock()
    mock_razorpay.create_recovery_payment_link.return_value = PaymentLinkResult(
        success=True, payment_link_id="plink_audit_1", short_url="https://rzp.io/i/1"
    )
    action = decide_action(event, classification)
    outcome = execute_action(event, classification, action, mock_razorpay)
    logger.log(outcome)

    reloaded = logger.load_all()
    assert len(reloaded) == 1
    assert reloaded[0].event_id == "evt_audit_1"
    assert reloaded[0].amount_offered == 1200.0


def test_audit_log_skips_corrupted_lines_without_crashing(tmp_path):
    """Edge case: what if the audit log file gets a corrupted/partial line
    (e.g. from a crash mid-write)? Loading must not blow up the whole dashboard."""
    log_path = tmp_path / "corrupted_audit.jsonl"
    log_path.write_text('{"not": "a valid RecoveryOutcome"}\n' + '{{{{broken json\n')

    logger = AuditLogger(log_path=str(log_path))
    result = logger.load_all()
    assert result == []  # both lines are unparseable, but no exception raised