"""
Payment-failure classifier — hybrid rule + LLM, scoped to PAYMENT_FAILURE scenario.

Distinct from the abandonment classifier (classifier.py) in one key way:
  - Abandonment: customer closed the window. The question is *why* they left.
  - Payment failure: a transaction was ATTEMPTED and a failure event was emitted.
    The payment_status_code is almost always the direct machine-readable answer.
    Rules therefore cover ~95% of real cases at confidence=1.0.
    LLM fallback handles novel/undocumented status codes from new gateways.

This module operates on a CheckoutSession DB row directly (not on the synthetic
CheckoutEvent shape) because payment failure sessions always have DB persistence
by the time we classify them. The session's payment_status_code is the primary signal.

Output: PaymentFailureClassification — a typed dataclass kept separate from the
abandonment ClassificationResult on purpose. They serve different downstream ladders
and must not be interchanged by accident.

Nothing in this module creates a RecoveryCase or takes any action. Classification only.
"""

from __future__ import annotations

import os
import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Literal

logger = logging.getLogger("recovery_agent.payment_failure_classifier")


# ---------------------------------------------------------------------------
# Rule table
# Each entry: payment_status_code -> {"source": ..., "class": ...}
#
# source: who / what caused the failure
#   "bank"     - issuing bank declined or expired card — needs alternate method
#   "customer" - customer action (wrong OTP, cancellation) — needs re-engagement
#   "gateway"  - transient infra failure — safe to retry with same method
#   "business" - risk engine / fraud block — do NOT auto-retry, human decision
#
# class: the recommended intervention category (used by the action ladder)
#   "needs_alternate_method"  - payment link with UPI / netbanking as lead CTA
#   "needs_customer_action"   - remind customer to re-enter OTP, re-initiate
#   "retryable_technical"     - retry same method, no discount needed
#   "risk_terminal"           - flag for manual review ONLY, no auto action
# ---------------------------------------------------------------------------

PAYMENT_FAILURE_RULES: dict[str, dict[str, str]] = {
    # ---- Bank / issuer failures ----
    # These are hard declines from the issuing bank. The card itself is the
    # problem; retrying the same card is futile and risks card-testing flags.
    "insufficient_funds": {
        "source": "bank",
        "class": "needs_alternate_method",
        "description": "Issuing bank declined: insufficient funds. Customer needs to try another card or UPI.",
    },
    "card_expired": {
        "source": "bank",
        "class": "needs_alternate_method",
        "description": "Card expired. Customer must use a different payment method.",
    },
    "issuer_decline": {
        "source": "bank",
        "class": "needs_alternate_method",
        "description": "Issuing bank declined without specific reason. Offer alternate payment method.",
    },

    # ---- Customer-action failures ----
    # Customer initiated but didn't complete correctly. Intent to pay is present.
    # Re-engagement (reminder + clear CTA) is the right move, not a new method.
    "otp_invalid": {
        "source": "customer",
        "class": "needs_customer_action",
        "description": "OTP entered incorrectly. Remind customer to re-initiate with correct OTP.",
    },
    "payment_cancelled": {
        "source": "customer",
        "class": "needs_customer_action",
        "description": "Customer explicitly cancelled payment. Light nudge to re-initiate, no discount.",
    },

    # ---- Gateway / infrastructure failures ----
    # Transient infra errors. No fault on card or customer. Safe to retry the
    # SAME method; a new payment link pointing to the same cart is sufficient.
    "gateway_timeout": {
        "source": "gateway",
        "class": "retryable_technical",
        "description": "Gateway timed out before receiving bank response. Safe to retry same method.",
    },
    "network_error": {
        "source": "gateway",
        "class": "retryable_technical",
        "description": "Network error mid-transaction. Retry with same method and cart value.",
    },

    # ---- Risk / business failures ----
    # Fraud / risk block. Auto-retry would re-trigger the block and create noise.
    # Human review required before any action. No auto-discount, no auto-link.
    "risk_blocked": {
        "source": "business",
        "class": "risk_terminal",
        "description": "Risk engine blocked the transaction. Requires manual review — no auto action.",
    },
}


# ---------------------------------------------------------------------------
# Result dataclass
# Kept separate from ClassificationResult (abandonment) — they're different
# schemas serving different downstream ladders.
# ---------------------------------------------------------------------------

@dataclass
class PaymentFailureClassification:
    """Classification output for a PAYMENT_FAILURE scenario.

    Fields:
      session_id       - CheckoutSession.id (int) or event_id (str) for correlation
      status_code      - The raw payment_status_code from the session
      source           - "bank" | "customer" | "gateway" | "business" | "unknown"
      failure_class    - "needs_alternate_method" | "needs_customer_action" |
                         "retryable_technical" | "risk_terminal" | "unknown"
      description      - Human-readable diagnosis for the audit trail
      confidence       - 1.0 for rule hits, 0-1 for LLM output, 0.0 on LLM error
      method           - "rule" | "llm"
      llm_raw          - Raw LLM output if method=="llm", else None (debug only)
    """
    session_id: str | int
    status_code: Optional[str]
    source: str                     # bank | customer | gateway | business | unknown
    failure_class: str              # needs_alternate_method | needs_customer_action | ...
    description: str
    confidence: float
    method: Literal["rule", "llm"]
    llm_raw: Optional[str] = field(default=None, repr=False)

    @property
    def is_auto_actionable(self) -> bool:
        """True when the classification does NOT require human approval before acting."""
        return self.failure_class != "risk_terminal"

    @property
    def discount_allowed(self) -> bool:
        """Payment failures must NEVER receive blind discounts — see the design constraint
        in the user's request. Discount is never appropriate for a transactional failure;
        the customer intended to pay the full price and was blocked by infrastructure or
        their bank, not by hesitation about the price."""
        return False


# ---------------------------------------------------------------------------
# LLM system prompt (payment-failure variant)
# Separate from the abandonment prompt because the reason vocabulary differs.
# ---------------------------------------------------------------------------

_PF_LLM_SYSTEM_PROMPT = """\
You are classifying a payment transaction failure for an e-commerce checkout.
A payment was ATTEMPTED (intent to pay existed) but failed with an unrecognised status code.

Based on the payment_status_code and any context, output a JSON classification:
{
  "source": "<one of: bank | customer | gateway | business | unknown>",
  "class": "<one of: needs_alternate_method | needs_customer_action | retryable_technical | risk_terminal | unknown>",
  "description": "<one sentence explaining the failure and recommended intervention>",
  "confidence": <float 0.0-1.0>
}

Definitions:
  source=bank       -> Issuing bank hard-declined (insufficient funds, expired, generic issuer decline)
  source=customer   -> Customer action caused it (wrong OTP, deliberate cancellation)
  source=gateway    -> Transient infra failure (timeout, network error) - safe to retry
  source=business   -> Risk/fraud block - requires human review, NO auto action
  source=unknown    -> Cannot determine from available signals

  class=needs_alternate_method -> Offer a different payment method (UPI, netbanking)
  class=needs_customer_action  -> Remind customer to re-initiate (OTP issue, accidental cancel)
  class=retryable_technical    -> Retry same method via new payment link, no discount
  class=risk_terminal          -> Flag for human review ONLY, do NOT auto-retry or discount
  class=unknown                -> Cannot determine

Respond with ONLY the JSON object. No markdown, no prose."""


from app.config import GEMINI_DEFAULT_MODEL, CLAUDE_DEFAULT_MODEL
from app.agent.classifier import _is_configuration_error


def _llm_classify_payment_failure(
    session_id: str | int,
    status_code: Optional[str],
    cart_value: float,
    llm_client=None,
) -> PaymentFailureClassification:
    """LLM fallback: called only when payment_status_code is not in PAYMENT_FAILURE_RULES."""

    context = {
        "scenario": "payment_failure",
        "payment_status_code": status_code,
        "cart_value": cart_value,
    }
    raw_text = None

    gemini_models_to_try = [GEMINI_DEFAULT_MODEL, "gemini-3.5-flash", "gemini-3.5-flash-lite"] if os.getenv("GEMINI_API_KEY") else []

    try:
        if os.getenv("GEMINI_API_KEY"):
            import google.generativeai as genai
            genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
            for attempt, model_name in enumerate(gemini_models_to_try):
                try:
                    model = genai.GenerativeModel(
                        model_name,
                        system_instruction=_PF_LLM_SYSTEM_PROMPT,
                    )
                    response = model.generate_content(
                        json.dumps(context),
                        generation_config={"temperature": 0.0},
                    )
                    raw_text = response.text.strip()
                    break
                except Exception as ge:
                    if ("429" in str(ge) or "quota" in str(ge).lower() or "rate" in str(ge).lower()) and attempt < len(gemini_models_to_try) - 1:
                        logger.warning("LLM rate limit (429) on %s with %s, falling back to next available model...", session_id, model_name)
                        continue
                    raise ge
        else:
            if llm_client is None:
                import anthropic
                llm_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
            response = llm_client.messages.create(
                model=CLAUDE_DEFAULT_MODEL,
                max_tokens=300,
                system=_PF_LLM_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(context)}],
            )
            raw_text = response.content[0].text.strip()

        # Strip markdown fences defensively
        if raw_text.startswith("```"):
            raw_text = raw_text.strip("`")
            if raw_text.startswith("json"):
                raw_text = raw_text[4:].strip()

        parsed = json.loads(raw_text)
        return PaymentFailureClassification(
            session_id=session_id,
            status_code=status_code,
            source=parsed.get("source", "unknown"),
            failure_class=parsed.get("class", "unknown"),
            description=parsed.get("description", "LLM classified — no description."),
            confidence=float(parsed.get("confidence", 0.5)),
            method="llm",
            llm_raw=raw_text,
        )

    except Exception as exc:
        if _is_configuration_error(exc):
            logger.error(
                "LLM CONFIGURATION ERROR — check model name/API key for session %s (code=%s): %s",
                session_id, status_code, exc,
            )
        else:
            logger.warning(
                "LLM transient API failure for session %s (code=%s): %s",
                session_id, status_code, exc,
            )
        return PaymentFailureClassification(
            session_id=session_id,
            status_code=status_code,
            source="unknown",
            failure_class="unknown",
            description="LLM classification unavailable — flagged for manual review.",
            confidence=0.0,
            method="llm",
            llm_raw=raw_text,
        )


# ---------------------------------------------------------------------------
# Source → class mapping for the source-only fallback path.
# Mirrors the rule table's conventions so the two paths are always consistent.
# ---------------------------------------------------------------------------

_SOURCE_TO_CLASS: dict[str, str] = {
    "bank":     "needs_alternate_method",
    "customer": "needs_customer_action",
    "gateway":  "retryable_technical",
    "business": "risk_terminal",
}

_SOURCE_TO_DESCRIPTION: dict[str, str] = {
    "bank":     "Issuing bank declined the transaction. Customer should try an alternate payment method.",
    "customer": "Customer action caused the failure. Light nudge to re-initiate payment.",
    "gateway":  "Transient gateway or network error. Safe to retry with the same method.",
    "business": "Risk/fraud engine blocked the transaction. Requires manual review — no auto action.",
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def classify_payment_failure(
    session,  # CheckoutSession ORM row (or any object with .id, .payment_status_code, .cart_value)
    llm_client=None,
    error_source: Optional[str] = None,
) -> PaymentFailureClassification:
    """Classify a payment failure from a CheckoutSession row.

    Three-tier strategy (highest → lowest priority):

    Tier 1 — Rule hit + source verification (confidence=1.0, method=rule):
        Look up payment_status_code in PAYMENT_FAILURE_RULES.
        If found AND error_source is absent or matches the rule's source, use the rule.
        If found BUT error_source contradicts the rule (e.g. rule says "bank" but
        Razorpay reported "business"), the real error_source WINS.  We keep the
        rule's class only if it's compatible; otherwise we route directly off the
        real source using _SOURCE_TO_CLASS (see tier 2 below).

    Tier 2 — Source-only classification (confidence=0.9, method=rule):
        If payment_status_code had no rule match but error_source is a recognised
        Razorpay source string ("bank"|"customer"|"gateway"|"business"), classify
        deterministically off the source map.  This handles generic Razorpay codes
        like "payment_failed" where the reason string alone is ambiguous but the
        source field carries the real signal.

    Tier 3 — LLM fallback (confidence variable, method=llm):
        Only reached when error_source is absent or unrecognised AND no rule
        matched the status code.  Adds the error_source to the context object so
        the LLM can use it if available.

    Args:
      session     - A CheckoutSession ORM instance (or duck-typed equivalent with
                    .id / .event_id, .payment_status_code, .cart_value).
      llm_client  - Optional pre-built Anthropic client (omit in production;
                    useful for tests to avoid hitting the real API).
      error_source - Razorpay's real error.source string forwarded from the
                     payment.failed event in cart.html.  When present it always
                     takes precedence over the rule table's inferred source.

    Returns:
      PaymentFailureClassification — classification only, no side effects.
    """
    # Prefer event_id for correlation; fall back to numeric id.
    session_id = getattr(session, "event_id", None) or getattr(session, "id", "unknown")
    status_code = getattr(session, "payment_status_code", None)
    cart_value = getattr(session, "cart_value", 0.0)

    # Normalise error_source: only accept the four Razorpay-defined values.
    recognised_sources = set(_SOURCE_TO_CLASS)
    real_source = error_source if error_source in recognised_sources else None

    # ------------------------------------------------------------------
    # Tier 1: Rule table lookup + source verification
    # ------------------------------------------------------------------
    if status_code and status_code in PAYMENT_FAILURE_RULES:
        rule = PAYMENT_FAILURE_RULES[status_code]
        rule_source = rule["source"]

        if real_source is None or real_source == rule_source:
            # Rule source and real source agree (or real source absent) — use rule as-is.
            logger.debug(
                "Rule hit for session %s: status_code=%s -> source=%s, class=%s "
                "(real_source=%s, no conflict)",
                session_id, status_code, rule_source, rule["class"], real_source,
            )
            return PaymentFailureClassification(
                session_id=session_id,
                status_code=status_code,
                source=rule_source,
                failure_class=rule["class"],
                description=rule["description"],
                confidence=1.0,
                method="rule",
            )
        else:
            # real_source contradicts the rule — Razorpay's source wins.
            resolved_class = _SOURCE_TO_CLASS[real_source]
            resolved_desc = _SOURCE_TO_DESCRIPTION[real_source]
            logger.info(
                "Rule conflict on session %s: status_code=%s rule_source=%s BUT "
                "real_error_source=%s — trusting Razorpay, routing to class=%s",
                session_id, status_code, rule_source, real_source, resolved_class,
            )
            return PaymentFailureClassification(
                session_id=session_id,
                status_code=status_code,
                source=real_source,                 # Razorpay's real source wins
                failure_class=resolved_class,
                description=(
                    f"Rule ({status_code} -> {rule_source}) overridden by Razorpay "
                    f"error.source={real_source}. {resolved_desc}"
                ),
                confidence=1.0,
                method="rule",
            )

    # ------------------------------------------------------------------
    # Tier 2: No rule match — classify off Razorpay's real error_source if present
    # ------------------------------------------------------------------
    if real_source is not None:
        resolved_class = _SOURCE_TO_CLASS[real_source]
        resolved_desc = _SOURCE_TO_DESCRIPTION[real_source]
        logger.info(
            "No rule for status_code=%s on session %s; "
            "classifying off real error_source=%s -> class=%s (confidence=0.9)",
            status_code, session_id, real_source, resolved_class,
        )
        return PaymentFailureClassification(
            session_id=session_id,
            status_code=status_code,
            source=real_source,
            failure_class=resolved_class,
            description=f"No rule match for '{status_code}'; classified via Razorpay error.source={real_source}. {resolved_desc}",
            confidence=0.9,
            method="rule",
        )

    # ------------------------------------------------------------------
    # Tier 3: LLM fallback — no rule, no recognisable source
    # ------------------------------------------------------------------
    if status_code:
        logger.info(
            "No rule for payment_status_code=%s on session %s "
            "and no recognised error_source — falling back to LLM.",
            status_code, session_id,
        )
    else:
        logger.info(
            "Session %s has no payment_status_code and no error_source — falling back to LLM.",
            session_id,
        )

    return _llm_classify_payment_failure(session_id, status_code, cart_value, llm_client)

