"""
Reconciliation: the ONLY place `confirmed_recovered_amount` gets set.

A payment link being created (amount_offered) is an opportunity, not revenue.
This module closes the loop by checking each link's real status against
Razorpay and only counting it as recovered if status == "paid".

In a real production system this would run on a schedule (poll or webhook),
since customers pay minutes/hours/days after receiving a link. For this build,
we run it as an explicit second pass after the main pipeline, which also
matches how you're manually marking test-mode payments as success/failure.
"""

import logging
from app.models import RecoveryOutcome
from app.razorpay_client import RazorpayRecoveryClient

logger = logging.getLogger("recovery_agent.reconciliation")

PAID_STATUS = "paid"


def reconcile_outcome(outcome: RecoveryOutcome, razorpay_client: RazorpayRecoveryClient) -> RecoveryOutcome:
    """Checks ONE outcome's real payment status and returns an updated copy.
    Only outcomes with a payment_link_id (i.e. a link was actually created) can be reconciled."""

    if outcome.payment_link_id is None:
        # Nothing to check -- this outcome never created a payment link
        # (e.g. it was a no-op guardrail action, or link creation failed).
        return outcome

    status = razorpay_client.fetch_payment_link_status(outcome.payment_link_id)

    if status is None:
        # Fetch itself failed (network/API issue) -- don't silently assume unpaid,
        # leave confirmed_recovered_amount as None and flag it, so this doesn't
        # get double-counted as "definitely not recovered" when we simply don't know.
        return outcome.model_copy(update={
            "error_message": (outcome.error_message or "") + " | Reconciliation check failed: status unavailable."
        })

    if status == PAID_STATUS:
        return outcome.model_copy(update={"confirmed_recovered_amount": outcome.amount_offered})

    # Any other status (created, cancelled, expired) means genuinely not recovered yet/at all.
    return outcome.model_copy(update={"confirmed_recovered_amount": None})


def reconcile_batch(outcomes: list[RecoveryOutcome], razorpay_client: RazorpayRecoveryClient) -> list[RecoveryOutcome]:
    reconciled = []
    for outcome in outcomes:
        try:
            reconciled.append(reconcile_outcome(outcome, razorpay_client))
        except Exception as e:
            # One bad reconciliation must never kill the whole batch.
            logger.error(f"Reconciliation failed unexpectedly for {outcome.event_id}: {e}")
            reconciled.append(outcome)
    return reconciled