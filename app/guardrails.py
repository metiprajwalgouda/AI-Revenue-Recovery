"""
Guardrails: hard rules that override classification/recovery logic entirely.

Design principle: these checks run FIRST, before we even bother classifying
the abandonment reason. If a guardrail fires, we skip straight to a safe
no-op action. This ordering matters -- it's what makes the agent "bounded"
rather than "smart but occasionally reckless."
"""

from models import CheckoutEvent, RecoveryAction

MAX_RECOVERY_ATTEMPTS = 2
MAX_DISCOUNT_PERCENT = 15  # hard ceiling the agent can never exceed


def check_guardrails(event: CheckoutEvent) -> RecoveryAction | None:
    """Returns a RecoveryAction if a guardrail blocks normal processing,
    or None if it's safe to proceed to classification + normal recovery logic."""

    if event.opted_out_of_marketing:
        return RecoveryAction.NO_ACTION_RESPECT_OPT_OUT

    if event.previous_recovery_attempts >= MAX_RECOVERY_ATTEMPTS:
        return RecoveryAction.NO_ACTION_MAX_RETRIES_REACHED

    if event.cart_value <= 0:
        # Zero/negative cart value should never trigger a payment link --
        # this is almost certainly bad data upstream, needs a human to look at it.
        return RecoveryAction.FLAG_FOR_MANUAL_REVIEW

    return None


def cap_discount(requested_percent: float) -> float:
    """Never let ANY code path (classifier, LLM, future feature) push a discount
    above the hard ceiling. This is intentionally a separate function so it's
    the single choke point -- easy to audit, easy to unit test in isolation."""
    return min(requested_percent, MAX_DISCOUNT_PERCENT)