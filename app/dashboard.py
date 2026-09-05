"""
Aggregates a batch of RecoveryOutcomes into the honest summary numbers
for your submission: abandoned value, offered vs confirmed recovered,
action breakdown, classification method split, and the exception list.
"""

from app.models import RecoveryOutcome, RecoveryAction


def compute_summary(outcomes: list[RecoveryOutcome], original_cart_values: dict[str, float]) -> dict:
    total_abandoned_value = sum(original_cart_values.values())
    total_offered = sum(o.amount_offered for o in outcomes if o.amount_offered is not None)
    total_confirmed_recovered = sum(
        o.confirmed_recovered_amount for o in outcomes if o.confirmed_recovered_amount is not None
    )

    action_breakdown = {}
    for o in outcomes:
        action_breakdown[o.action_taken.value] = action_breakdown.get(o.action_taken.value, 0) + 1

    method_breakdown = {"rule": 0, "llm": 0}
    for o in outcomes:
        m = o.classification.method_used
        if m in method_breakdown:
            method_breakdown[m] += 1
        elif str(m).startswith("llm"):
            method_breakdown["llm"] += 1
        else:
            method_breakdown[m] = method_breakdown.get(m, 0) + 1

    exceptions = [
        {"event_id": o.event_id, "action": o.action_taken.value, "error": o.error_message}
        for o in outcomes
        if not o.action_success or o.error_message
    ]

    guardrail_blocked = sum(
        1 for o in outcomes
        if o.action_taken in (
            RecoveryAction.NO_ACTION_RESPECT_OPT_OUT,
            RecoveryAction.NO_ACTION_MAX_RETRIES_REACHED,
        )
    )

    recovery_rate_of_abandoned = (
        (total_confirmed_recovered / total_abandoned_value * 100) if total_abandoned_value > 0 else 0.0
    )
    link_paid_rate = (
        (total_confirmed_recovered / total_offered * 100) if total_offered > 0 else 0.0
    )

    return {
        "total_events": len(outcomes),
        "total_abandoned_value": round(total_abandoned_value, 2),
        "total_amount_offered": round(total_offered, 2),
        "total_confirmed_recovered": round(total_confirmed_recovered, 2),
        "recovery_rate_of_total_abandoned_pct": round(recovery_rate_of_abandoned, 2),
        "link_paid_rate_pct": round(link_paid_rate, 2),
        "action_breakdown": action_breakdown,
        "classification_method_breakdown": method_breakdown,
        "guardrail_blocked_count": guardrail_blocked,
        "exceptions": exceptions,
    }