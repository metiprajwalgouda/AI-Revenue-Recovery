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


from app.config import GEMINI_DEFAULT_MODEL, CLAUDE_DEFAULT_MODEL


def _is_configuration_error(exc: Exception) -> bool:
    """Checks whether the exception is a permanent 4xx / auth / model-not-found / bad-request configuration error (excluding 429 rate limits)."""
    exc_str = str(exc).lower()
    if "429" in exc_str or "quota" in exc_str or "rate limit" in exc_str or "resource_exhausted" in exc_str:
        return False

    status_code = getattr(exc, "status_code", getattr(exc, "code", None))
    if status_code is not None:
        try:
            code_int = int(status_code)
            if code_int == 429:
                return False
            if 400 <= code_int < 500:
                return True
        except Exception:
            pass

    config_indicators = [
        "404", "400", "401", "403", "not found", "not_found",
        "permission_denied", "invalid_argument", "unauthenticated",
        "authentication", "api_key", "model not supported", "is not found",
        "invalid model", "does not exist", "bad request",
    ]
    return any(ind in exc_str for ind in config_indicators)


def llm_classify(event: CheckoutEvent, client=None) -> ClassificationResult:
    """Uses Gemini (primary) or Claude (fallback on 429/quota error) to classify ambiguous cases."""
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

    raw_text = None
    method_used = "llm"
    gemini_quota_error = False

    # 1. If an explicit client is passed (e.g. tests or custom injection), use it directly
    if client is not None:
        try:
            response = client.messages.create(
                model=CLAUDE_DEFAULT_MODEL,
                max_tokens=200,
                system=LLM_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(event_summary)}],
            )
            raw_text = response.content[0].text.strip()
            method_used = "llm"
        except Exception as e:
            logger.warning("Explicit LLM client call failed for %s: %s", event.event_id, e)

    # 2. Otherwise try Gemini primary (if GEMINI_API_KEY is configured)
    elif os.getenv("GEMINI_API_KEY"):
        gemini_models_to_try = [GEMINI_DEFAULT_MODEL, "gemini-3.5-flash", "gemini-3.5-flash-lite"]
        for attempt, model_name in enumerate(gemini_models_to_try):
            try:
                import google.generativeai as genai
                genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
                model = genai.GenerativeModel(model_name, system_instruction=LLM_SYSTEM_PROMPT)
                response = model.generate_content(
                    json.dumps(event_summary),
                    generation_config={"temperature": 0.0},
                )
                raw_text = response.text.strip()
                method_used = "llm_gemini"
                break
            except Exception as e:
                exc_str = str(e).lower()
                is_quota = (
                    "429" in exc_str
                    or "quota" in exc_str
                    or "resource_exhausted" in exc_str
                    or "rate" in exc_str
                )
                if is_quota:
                    gemini_quota_error = True
                    logger.warning(
                        "Gemini quota/429 error on %s with %s (attempt %d/%d)",
                        event.event_id, model_name, attempt + 1, len(gemini_models_to_try),
                    )
                    continue
                elif _is_configuration_error(e):
                    logger.error("Gemini configuration error for %s: %s", event.event_id, e)
                    break
                else:
                    logger.warning("Gemini transient API failure for %s: %s", event.event_id, e)
                    break

    # 3. Fallback to Claude if:
    #    a) Gemini failed specifically with a 429/quota error, OR
    #    b) GEMINI_API_KEY was not configured and no client was passed
    if raw_text is None and (gemini_quota_error or not os.getenv("GEMINI_API_KEY")) and client is None:
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if anthropic_key:
            try:
                if gemini_quota_error:
                    logger.warning(
                        "Gemini hit 429 quota limit; falling back to Claude (%s) for %s...",
                        CLAUDE_DEFAULT_MODEL, event.event_id,
                    )
                import anthropic
                anthropic_client = anthropic.Anthropic(api_key=anthropic_key)
                response = anthropic_client.messages.create(
                    model=CLAUDE_DEFAULT_MODEL,
                    max_tokens=200,
                    system=LLM_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": json.dumps(event_summary)}],
                )
                raw_text = response.content[0].text.strip()
                method_used = "llm_claude_fallback" if gemini_quota_error else "llm"
            except Exception as ce:
                logger.warning("Claude fallback call failed for %s: %s", event.event_id, ce)

    # 4. Parse response if successfully obtained from either provider
    if raw_text:
        try:
            clean_text = raw_text
            if clean_text.startswith("```"):
                clean_text = clean_text.strip("`")
                if clean_text.startswith("json"):
                    clean_text = clean_text[4:].strip()

            parsed = json.loads(clean_text)
            return ClassificationResult(
                event_id=event.event_id,
                predicted_reason=AbandonmentReason(parsed["predicted_reason"]),
                confidence=float(parsed["confidence"]),
                method_used=method_used,
                reasoning=parsed["reasoning"],
            )
        except Exception as pe:
            logger.warning("Failed to parse LLM JSON response for %s: %s (raw: %r)", event.event_id, pe, raw_text)

    # 5. Fall back to unknown only if BOTH providers fail
    return ClassificationResult(
        event_id=event.event_id,
        predicted_reason=AbandonmentReason.UNKNOWN,
        confidence=0.0,
        method_used="llm",
        reasoning="Diagnosis unavailable — flagged for manual review.",
    )


def classify(event: CheckoutEvent, llm_client=None) -> ClassificationResult:
    """Main entry point: try rules first, fall back to LLM."""
    rule_result = rule_based_classify(event)
    if rule_result is not None:
        return rule_result
    return llm_classify(event, client=llm_client)