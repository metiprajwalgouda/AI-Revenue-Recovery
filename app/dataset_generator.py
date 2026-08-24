"""
Generates a realistic synthetic dataset of abandoned checkout events.

Design principle: don't just randomize everything. Each `true_reason` should
produce a CONSISTENT, realistic pattern of signals -- this is what makes the
dataset usable for testing a classifier, instead of being pure noise.

We also deliberately inject edge cases (see EDGE_CASE_COUNT block) so the
agent has real failure modes to handle -- this becomes your CHALLENGES.md material.
"""

import json
import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from faker import Faker
from app.models import AbandonmentReason, PaymentMethod

fake = Faker("en_IN")
random.seed(42)  # reproducible dataset -- important for honest before/after comparisons


def _ts(base: datetime, minutes_offset: int) -> datetime:
    return base + timedelta(minutes=minutes_offset)


def _build_normal_event(reason: AbandonmentReason) -> dict:
    """Builds one event whose signals are CONSISTENT with the given true_reason.
    This mimics what real signal patterns look like for each cause."""
    event_id = f"evt_{uuid.uuid4().hex[:10]}"
    customer_id = f"cust_{uuid.uuid4().hex[:8]}"
    started = fake.date_time_between(start_date="-14d", end_date="now")
    cart_value = round(random.uniform(299, 45000), 2)
    method = random.choice(list(PaymentMethod))

    base = {
        "event_id": event_id,
        "customer_id": customer_id,
        "customer_email": fake.email(),
        "customer_phone": f"+91{random.randint(7000000000, 9999999999)}",
        "cart_value": cart_value,
        "payment_method_attempted": method.value,
        "checkout_started_at": started.isoformat(),
        "true_reason": reason.value,
        "opted_out_of_marketing": random.random() < 0.08,   # ~8% base rate
        "previous_recovery_attempts": 0,
        "payment_status_code": None,
        "page_load_time_ms": random.randint(400, 1200),
        "otp_requested": False,
        "otp_verified": False,
        "time_on_checkout_page_sec": random.randint(30, 180),
        "notes": None,
    }

    if reason == AbandonmentReason.CARD_DECLINED:
        base["payment_status_code"] = random.choice(
            ["insufficient_funds", "card_declined_by_bank", "invalid_cvv"]
        )
        base["abandoned_at"] = _ts(started, random.randint(2, 6)).isoformat()

    elif reason == AbandonmentReason.OTP_TIMEOUT:
        base["otp_requested"] = True
        base["otp_verified"] = False
        base["abandoned_at"] = _ts(started, random.randint(3, 8)).isoformat()

    elif reason == AbandonmentReason.PAGE_LOAD_SLOW:
        base["page_load_time_ms"] = random.randint(6000, 15000)
        base["abandoned_at"] = _ts(started, 1).isoformat()

    elif reason == AbandonmentReason.HIGH_AMOUNT_HESITATION:
        base["cart_value"] = round(random.uniform(20000, 80000), 2)
        base["time_on_checkout_page_sec"] = random.randint(180, 600)
        base["abandoned_at"] = _ts(started, random.randint(5, 12)).isoformat()

    elif reason == AbandonmentReason.NETWORK_DROP:
        base["payment_status_code"] = "gateway_timeout"
        base["abandoned_at"] = _ts(started, random.randint(1, 3)).isoformat()

    elif reason == AbandonmentReason.PRICE_SHOCK_AT_CHECKOUT:
        base["notes"] = "Customer chat: 'why is shipping so expensive suddenly'"
        base["time_on_checkout_page_sec"] = random.randint(10, 40)
        base["abandoned_at"] = _ts(started, random.randint(1, 2)).isoformat()

    elif reason == AbandonmentReason.ACCIDENTAL_CLOSE:
        base["time_on_checkout_page_sec"] = random.randint(2, 15)
        base["abandoned_at"] = _ts(started, 1).isoformat()

    else:  # UNKNOWN -- genuinely ambiguous, this is what the LLM branch is FOR
        base["notes"] = random.choice([
            "Customer chat: 'let me check with my wife and get back'",
            None,
            "support ticket: payment page looked weird, not sure what happened",
        ])
        base["abandoned_at"] = _ts(started, random.randint(2, 20)).isoformat()

    return base


def _build_edge_case_events() -> list[dict]:
    """Deliberately broken / weird records the agent MUST handle without crashing.
    Each one maps to something you'll document in CHALLENGES.md."""
    edge_cases = []
    started = fake.date_time_between(start_date="-14d", end_date="now")

    # 1. Duplicate customer/event pair (tests dedup logic)
    dup_customer = f"cust_{uuid.uuid4().hex[:8]}"
    for i in range(2):
        edge_cases.append({
            "event_id": f"evt_dup_{i}_{uuid.uuid4().hex[:6]}",
            "customer_id": dup_customer,
            "customer_email": fake.email(),
            "customer_phone": f"+91{random.randint(7000000000, 9999999999)}",
            "cart_value": 1499.0,
            "payment_method_attempted": "upi",
            "checkout_started_at": started.isoformat(),
            "abandoned_at": _ts(started, 3).isoformat(),
            "true_reason": AbandonmentReason.OTP_TIMEOUT.value,
            "opted_out_of_marketing": False,
            "previous_recovery_attempts": 0,
            "payment_status_code": None,
            "page_load_time_ms": 800,
            "otp_requested": True,
            "otp_verified": False,
            "time_on_checkout_page_sec": 45,
            "notes": None,
        })

    # 2. Zero / negative amount (tests input validation)
    edge_cases.append({
        "event_id": f"evt_edge_{uuid.uuid4().hex[:6]}",
        "customer_id": f"cust_{uuid.uuid4().hex[:8]}",
        "customer_email": fake.email(),
        "customer_phone": f"+91{random.randint(7000000000, 9999999999)}",
        "cart_value": 0.0,
        "payment_method_attempted": "card",
        "checkout_started_at": started.isoformat(),
        "abandoned_at": _ts(started, 2).isoformat(),
        "true_reason": AbandonmentReason.UNKNOWN.value,
        "opted_out_of_marketing": False,
        "previous_recovery_attempts": 0,
        "payment_status_code": None,
        "page_load_time_ms": 900,
        "otp_requested": False,
        "otp_verified": False,
        "time_on_checkout_page_sec": 20,
        "notes": "cart_value is zero -- should never reach payment, test guardrail",
    })

    # 3. Customer opted out but has multiple prior attempts (tests opt-out guardrail)
    edge_cases.append({
        "event_id": f"evt_edge_{uuid.uuid4().hex[:6]}",
        "customer_id": f"cust_{uuid.uuid4().hex[:8]}",
        "customer_email": fake.email(),
        "customer_phone": f"+91{random.randint(7000000000, 9999999999)}",
        "cart_value": 2599.0,
        "payment_method_attempted": "netbanking",
        "checkout_started_at": started.isoformat(),
        "abandoned_at": _ts(started, 4).isoformat(),
        "true_reason": AbandonmentReason.CARD_DECLINED.value,
        "opted_out_of_marketing": True,
        "previous_recovery_attempts": 1,
        "payment_status_code": "insufficient_funds",
        "page_load_time_ms": 700,
        "otp_requested": False,
        "otp_verified": False,
        "time_on_checkout_page_sec": 60,
        "notes": None,
    })

    # 4. Already at max retry limit (tests retry-cap guardrail)
    edge_cases.append({
        "event_id": f"evt_edge_{uuid.uuid4().hex[:6]}",
        "customer_id": f"cust_{uuid.uuid4().hex[:8]}",
        "customer_email": fake.email(),
        "customer_phone": f"+91{random.randint(7000000000, 9999999999)}",
        "cart_value": 899.0,
        "payment_method_attempted": "wallet",
        "checkout_started_at": started.isoformat(),
        "abandoned_at": _ts(started, 5).isoformat(),
        "true_reason": AbandonmentReason.OTP_TIMEOUT.value,
        "opted_out_of_marketing": False,
        "previous_recovery_attempts": 2,  # assume max = 2
        "payment_status_code": None,
        "page_load_time_ms": 750,
        "otp_requested": True,
        "otp_verified": False,
        "time_on_checkout_page_sec": 50,
        "notes": None,
    })

    # 5. Missing/malformed optional fields (tests robustness to sparse data)
    edge_cases.append({
        "event_id": f"evt_edge_{uuid.uuid4().hex[:6]}",
        "customer_id": f"cust_{uuid.uuid4().hex[:8]}",
        "customer_email": fake.email(),
        "customer_phone": f"+91{random.randint(7000000000, 9999999999)}",
        "cart_value": 3200.0,
        "payment_method_attempted": "card",
        "checkout_started_at": started.isoformat(),
        "abandoned_at": _ts(started, 2).isoformat(),
        "true_reason": AbandonmentReason.UNKNOWN.value,
        "opted_out_of_marketing": False,
        "previous_recovery_attempts": 0,
        "payment_status_code": None,
        "page_load_time_ms": None,   # missing signal on purpose
        "otp_requested": False,
        "otp_verified": False,
        "time_on_checkout_page_sec": None,  # missing signal on purpose
        "notes": None,
    })

    return edge_cases


def generate_dataset(n_normal: int = 80, out_path: str = "data/synthetic_events.json") -> list[dict]:
    """Generates n_normal realistic events + a fixed set of edge cases, writes to JSON."""
    reasons = list(AbandonmentReason)
    # Weighted distribution -- realistic e-commerce mix, not uniform random
    weights = {
        AbandonmentReason.CARD_DECLINED: 0.22,
        AbandonmentReason.OTP_TIMEOUT: 0.18,
        AbandonmentReason.PAGE_LOAD_SLOW: 0.10,
        AbandonmentReason.HIGH_AMOUNT_HESITATION: 0.12,
        AbandonmentReason.NETWORK_DROP: 0.10,
        AbandonmentReason.PRICE_SHOCK_AT_CHECKOUT: 0.10,
        AbandonmentReason.ACCIDENTAL_CLOSE: 0.08,
        AbandonmentReason.UNKNOWN: 0.10,
    }

    events = []
    for reason, weight in weights.items():
        count = round(n_normal * weight)
        for _ in range(count):
            events.append(_build_normal_event(reason))

    events.extend(_build_edge_case_events())
    random.shuffle(events)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(events, indent=2, default=str))
    print(f"Generated {len(events)} events ({n_normal} normal + {len(_build_edge_case_events())} edge cases) -> {out_path}")
    return events


if __name__ == "__main__":
    generate_dataset()