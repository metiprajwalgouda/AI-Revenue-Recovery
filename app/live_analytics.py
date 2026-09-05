"""
Computes dashboard analytics from REAL checkout sessions + recovery outcomes
(as opposed to app/dashboard.py, which summarizes the SYNTHETIC batch pipeline).

SCOPE NOTE (documented honestly, not hidden): CheckoutSession is not scoped to
a single merchant -- the public storefront is marketplace-style, so a single
cart/session can contain products from multiple merchants. Splitting these
numbers accurately per-merchant would require per-line-item merchant
attribution, which is real added scope beyond what this demo needs. These
analytics are therefore PLATFORM-WIDE (all merchants combined), not filtered
to "my store's orders only" -- shown as such in the dashboard UI rather than
mislabeled as per-merchant data.
"""

from sqlalchemy.orm import Session
from app.db_models import CheckoutSession, RecoveryOutcomeRecord, SessionStatus, RecoveryCase, CaseStatus, RecoveryScenario


def compute_confirmed_discounts(db: Session = None, merchant_id: int = None, cases: list[RecoveryCase] = None) -> float:
    """Computes confirmed/realized discount amount strictly from recovered cases (RecoveryCase.discount_amount)."""
    if cases is None:
        if db is None:
            return 0.0
        query = db.query(RecoveryCase)
        if merchant_id is not None:
            query = query.filter(RecoveryCase.merchant_id == merchant_id)
        cases = query.all()

    return round(
        sum(
            float(c.discount_amount or 0.0)
            for c in cases
            if c.status in (CaseStatus.RECOVERED, "recovered", "RECOVERED")
            or (c.amount_recovered and c.amount_recovered > 0)
        ),
        2,
    )


def compute_live_analytics(db: Session, merchant_id: int = None) -> dict:
    cases_query = db.query(RecoveryCase)
    if merchant_id is not None:
        cases_query = cases_query.filter(RecoveryCase.merchant_id == merchant_id)
    cases = cases_query.all()

    sessions = db.query(CheckoutSession).all()
    outcomes = db.query(RecoveryOutcomeRecord).all()

    started_count = sum(1 for s in sessions if s.status == SessionStatus.STARTED)
    completed_count = sum(1 for s in sessions if s.status == SessionStatus.COMPLETED)
    abandoned_count = sum(1 for s in sessions if s.status == SessionStatus.ABANDONED)

    total_abandoned_value = sum(s.cart_value for s in sessions if s.status == SessionStatus.ABANDONED)
    
    # Calculate confirmed discount cost strictly from recovered cases (realized redemptions)
    total_confirmed_discounts = compute_confirmed_discounts(db, merchant_id=merchant_id, cases=cases)

    # Confirmed recovered from RecoveryCase rows (ground truth) and RecoveryOutcomeRecord
    recovered_cases = [
        c for c in cases
        if c.status in (CaseStatus.RECOVERED, "recovered", "RECOVERED")
        or (c.amount_recovered and c.amount_recovered > 0)
    ]
    recovered_from_cases = sum(c.amount_recovered for c in recovered_cases if c.amount_recovered)
    outcome_recovered = sum(o.confirmed_recovered_amount for o in outcomes if o.confirmed_recovered_amount is not None)
    total_confirmed_recovered = max(recovered_from_cases, outcome_recovered)

    # Lost cases across all scenarios
    lost_cases = [c for c in cases if c.status in (CaseStatus.LOST, "lost", "LOST")]
    total_lost_value = sum(c.amount_at_risk for c in lost_cases if c.amount_at_risk)
    lost_count = len(lost_cases)

    # Base for rate calculation: abandoned session value or total pool
    rate_denominator = total_abandoned_value if total_abandoned_value > 0 else (total_confirmed_recovered + total_lost_value)
    recovery_rate_pct = (
        round(total_confirmed_recovered / rate_denominator * 100, 1)
        if rate_denominator > 0 else 0.0
    )

    action_breakdown: dict[str, int] = {}
    method_breakdown = {"rule": 0, "llm": 0}
    for o in outcomes:
        action_breakdown[o.action_taken] = action_breakdown.get(o.action_taken, 0) + 1
        if o.classification_method in method_breakdown:
            method_breakdown[o.classification_method] += 1
        elif o.classification_method and str(o.classification_method).startswith("llm"):
            method_breakdown["llm"] += 1

    recent_outcomes = sorted(outcomes, key=lambda o: o.created_at, reverse=True)[:10]
    
    recent_outcomes_list = []
    for o in recent_outcomes:
        recent_outcomes_list.append({
            "event_id": o.session.event_id if o.session else "?",
            "predicted_reason": o.predicted_reason,
            "confidence": o.confidence,
            "method": o.classification_method,
            "action_taken": o.action_taken,
            "action_success": o.action_success,
            "amount_offered": o.amount_offered,
            "confirmed_recovered_amount": o.confirmed_recovered_amount,
        })

    return {
        "started_count": started_count,
        "completed_count": completed_count,
        "abandoned_count": abandoned_count,
        "total_abandoned_value": round(total_abandoned_value, 2),
        "total_confirmed_discounts": round(total_confirmed_discounts, 2),
        "total_discount_cost": round(total_confirmed_discounts, 2),
        "total_amount_offered": round(total_confirmed_discounts, 2),
        "total_confirmed_recovered": round(total_confirmed_recovered, 2),
        "total_lost_value": round(total_lost_value, 2),
        "lost_count": lost_count,
        "recovery_rate_pct": recovery_rate_pct,
        "action_breakdown": action_breakdown,
        "classification_method_breakdown": method_breakdown,
        "recent_outcomes": recent_outcomes_list,
    }