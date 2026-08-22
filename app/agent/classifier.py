"""
Hybrid classifier for checkout abandonment reasons.

Strategy:
1. Try rule-based classification first (fast, free, deterministic).
   These rules only fire when signals are UNAMBIGUOUS.
2. If no rule matches confidently, fall back to the LLM (Claude Haiku),
   which reads the full event context (including free-text notes) and reasons
   about the likely cause.

This split matters for the "why now" story: most e-commerce abandonment has
clear structured signals (a rule can catch it instantly and for free). Only
the genuinely ambiguous ~10-15% needs an LLM's judgment. Throwing an LLM at
100% of cases would be slower and more expensive for no accuracy gain on the
easy cases.
"""

import os
import json
import logging
from app.models import CheckoutEvent, ClassificationResult, AbandonmentReason

logger = logging.getLogger("recovery_agent.classifier")


def rule_based_classify(event: CheckoutEvent) -> ClassificationResult | None:
    """Returns a confident classification if signals are clear, else None
    (meaning: defer to the LLM)."""

    if event.payment_status_code in ("insufficient_funds", "card_declined_by_bank", "invalid_cvv"):
        return ClassificationResult(
            event_id=event.event_id,
            predicted_reason=AbandonmentReason.CARD_DECLINED,
            confidence=0.95,
            method_used="rule",
            reasoning=f"payment_status_code='{event.payment_status_code}' is an explicit decline signal.",
        )

    if event.payment_status_code == "gateway_timeout":
        return ClassificationResult(
            event_id=event.event_id,
            predicted_reason=AbandonmentReason.NETWORK_DROP,
            confidence=0.9,
            method_used="rule",
            reasoning="payment_status_code='gateway_timeout' indicates a network/gateway failure, not customer choice.",
        )

    if event.otp_requested and not event.otp_verified:
        return ClassificationResult(
            event_id=event.event_id,
            predicted_reason=AbandonmentReason.OTP_TIMEOUT,
            confidence=0.9,
            method_used="rule",
            reasoning="OTP was requested but never verified before abandonment.",
        )

    if event.page_load_time_ms is not None and event.page_load_time_ms > 5000:
        return ClassificationResult(
            event_id=event.event_id,
            predicted_reason=AbandonmentReason.PAGE_LOAD_SLOW,
            confidence=0.85,
            method_used="rule",
            reasoning=f"page_load_time_ms={event.page_load_time_ms} exceeds 5000ms threshold.",
        )

    if event.cart_value >= 20000 and (event.time_on_checkout_page_sec or 0) >= 180:
        return ClassificationResult(
            event_id=event.event_id,
            predicted_reason=AbandonmentReason.HIGH_AMOUNT_HESITATION,
            confidence=0.75,
            method_used="rule",
            reasoning=f"High cart value (₹{event.cart_value}) with long dwell time "
                      f"({event.time_on_checkout_page_sec}s) suggests hesitation, not a technical failure.",
        )

    if (
        event.time_on_checkout_page_sec is not None
        and event.time_on_checkout_page_sec <= 5
        and not event.payment_status_code
        and not event.otp_requested
    ):
        return ClassificationResult(
            event_id=event.event_id,
            predicted_reason=AbandonmentReason.ACCIDENTAL_CLOSE,
            confidence=0.7,
            method_used="rule",
            reasoning="Extremely short time on page with no payment attempt or OTP suggests accidental close, not a deliberate decision.",
        )

    # No rule matched confidently -- genuinely ambiguous, defer to LLM
    return None


LLM_SYSTEM_PROMPT = """You are classifying why an e-commerce customer abandoned checkout.
Given structured signals about the session, choose the SINGLE most likely reason from this exact list:
card_declined, otp_timeout, page_load_slow, high_amount_hesitation, network_drop, price_shock_at_checkout, accidental_close, unknown

Respond with ONLY a JSON object, no other text, no markdown fences:
{"predicted_reason": "<one of the values above>", "confidence": <float 0-1>, "reasoning": "<one sentence>"}

If genuinely no reason fits well, use "unknown" with low confidence rather than guessing forcefully."""


def llm_classify(event: CheckoutEvent, client=None) -> ClassificationResult:
    """Uses Claude to classify ambiguous cases where rules didn't produce a confident answer.
    Accepts an optional pre-built client so this function is mockable/testable without
    hitting the real API in unit tests."""

    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    event_summary = {
        "cart_value": event.cart_value,
        "payment_method_attempted": event.payment_method_attempted,
        "payment_status_code": event.payment_status_code,
        "page_load_time_ms": event.page_load_time_ms,
        "otp_requested": event.otp_requested,
        "otp_verified": event.otp_verified,
        "time_on_checkout_page_sec": event.time_on_checkout_page_sec,
        "notes": event.notes,
    }

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=LLM_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(event_summary)}],
        )
        raw_text = response.content[0].text.strip()
        # Defensive parsing: LLMs occasionally wrap output in markdown fences
        # despite instructions -- strip them rather than crashing the batch.
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`")
            if raw_text.startswith("json"):
                raw_text = raw_text[4:].strip()

        parsed = json.loads(raw_text)

        return ClassificationResult(
            event_id=event.event_id,
            predicted_reason=AbandonmentReason(parsed["predicted_reason"]),
            confidence=float(parsed["confidence"]),
            method_used="llm",
            reasoning=parsed["reasoning"],
        )

    except (json.JSONDecodeError, KeyError, ValueError) as e:
        # LLM returned something we couldn't parse -- fail SAFE, don't crash the batch.
        logger.error(f"LLM classification parse failure for {event.event_id}: {e}")
        return ClassificationResult(
            event_id=event.event_id,
            predicted_reason=AbandonmentReason.UNKNOWN,
            confidence=0.0,
            method_used="llm",
            reasoning=f"LLM response could not be parsed, defaulted to unknown. Error: {e}",
        )

    except Exception as e:
        # API-level failure: timeout, rate limit, auth error, etc.
        logger.error(f"LLM API call failed for {event.event_id}: {e}")
        return ClassificationResult(
            event_id=event.event_id,
            predicted_reason=AbandonmentReason.UNKNOWN,
            confidence=0.0,
            method_used="llm",
            reasoning=f"LLM API call failed, defaulted to unknown. Error: {e}",
        )


def classify(event: CheckoutEvent, llm_client=None) -> ClassificationResult:
    """Main entry point: try rules first, fall back to LLM."""
    rule_result = rule_based_classify(event)
    if rule_result is not None:
        return rule_result
    return llm_classify(event, client=llm_client)