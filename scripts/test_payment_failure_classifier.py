"""
Test script for classify_payment_failure().

Runs the classifier against synthetic CheckoutSession-like objects covering:
  - Every entry in PAYMENT_FAILURE_RULES (rule path, expected confidence=1.0)
  - A status code NOT in the table (LLM fallback path — no real API call needed
    when MOCK_LLM=true is set, handled by the mock client below)
  - session with no payment_status_code at all (LLM fallback)

Usage:
    .venv_new\Scripts\python.exe scripts\test_payment_failure_classifier.py

Set MOCK_LLM=true (default) to skip real API calls.
Set MOCK_LLM=false to exercise the real LLM on the fallback cases.
"""

import os
import sys
import json

# Make sure project root is importable regardless of where this script is run
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Suppress noisy SQLAlchemy startup logs
os.environ.setdefault("MOCK_PAYMENTS", "true")
os.environ.setdefault("RECOVERY_MODE", "simulated")

from app.agent.payment_failure_classifier import (
    PAYMENT_FAILURE_RULES,
    PaymentFailureClassification,
    classify_payment_failure,
)

# ---------------------------------------------------------------------------
# Minimal duck-typed stand-in for a CheckoutSession ORM row
# (avoids needing a real DB connection for a classification-only test)
# ---------------------------------------------------------------------------

class FakeSession:
    def __init__(self, event_id, payment_status_code, cart_value=999.0):
        self.event_id = event_id
        self.id = int(hash(event_id) % 100000)
        self.payment_status_code = payment_status_code
        self.cart_value = cart_value


# ---------------------------------------------------------------------------
# Mock LLM client — returns a deterministic "unknown" classification for
# any status code not in the rules table, with no API call.
# ---------------------------------------------------------------------------

class MockLLMClient:
    """Mimics the Anthropic client interface. Returns valid JSON for any input."""
    class _Msg:
        def __init__(self, text):
            self.content = [type("C", (), {"text": text})()]

    def messages_create(self, **kwargs):
        # Return a safe "unknown" classification for unrecognised codes
        return self._Msg(json.dumps({
            "source": "unknown",
            "class": "unknown",
            "description": "[MOCK] Status code not recognised — flagged for manual review.",
            "confidence": 0.2,
        }))

    # Alias used by the classifier (messages.create)
    class _MsgsProxy:
        def __init__(self, outer):
            self._outer = outer
        def create(self, **kwargs):
            return outer._Msg(json.dumps({
                "source": "unknown",
                "class": "unknown",
                "description": "[MOCK] Status code not recognised — flagged for manual review.",
                "confidence": 0.2,
            }))

    def __init__(self):
        outer = self
        class _Msgs:
            def create(self, **kwargs):
                return outer._Msg(json.dumps({
                    "source": "unknown",
                    "class": "unknown",
                    "description": "[MOCK] Status code not recognised — flagged for manual review.",
                    "confidence": 0.2,
                }))
        self.messages = _Msgs()


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

RULE_CASES = [
    # (status_code, expected_source, expected_class)
    ("insufficient_funds",  "bank",     "needs_alternate_method"),
    ("card_expired",        "bank",     "needs_alternate_method"),
    ("issuer_decline",      "bank",     "needs_alternate_method"),
    ("otp_invalid",         "customer", "needs_customer_action"),
    ("payment_cancelled",   "customer", "needs_customer_action"),
    ("gateway_timeout",     "gateway",  "retryable_technical"),
    ("network_error",       "gateway",  "retryable_technical"),
    ("risk_blocked",        "business", "risk_terminal"),
]

LLM_FALLBACK_CASES = [
    # status codes not in the rules table AND no error_source → LLM handles
    ("3ds_auth_failed",    "unknown for LLM to handle"),
    ("bank_server_error",  "unknown for LLM to handle"),
    (None,                 "no status code at all"),
]

# New: Tier-2 path — status_code not in rules but error_source IS known.
# Must use method=rule, confidence=0.9, NO LLM call.
ERROR_SOURCE_ONLY_CASES = [
    # (status_code,    error_source,  expected_class,            expected_source)
    # THE critical case: generic "payment_failed" reason + source=business → risk_terminal
    ("payment_failed", "business",   "risk_terminal",            "business"),
    ("payment_failed", "bank",       "needs_alternate_method",   "bank"),
    ("payment_failed", "customer",   "needs_customer_action",    "customer"),
    ("payment_failed", "gateway",    "retryable_technical",      "gateway"),
    # Unrecognised reason, recognisable source
    ("some_new_code",  "bank",       "needs_alternate_method",   "bank"),
]

# New: Tier-1 conflict — rule hit but error_source contradicts rule; Razorpay wins.
# e.g. "issuer_decline" rule says source=bank, but Razorpay reports source=business
# → must classify as risk_terminal (business wins), not needs_alternate_method.
RULE_OVERRIDE_CASES = [
    # (status_code,    error_source,  expected_class,   expected_source)
    ("issuer_decline", "business",   "risk_terminal",   "business"),
    ("gateway_timeout","bank",       "needs_alternate_method", "bank"),
]


def print_result(result: PaymentFailureClassification, prefix: str = "") -> None:
    print(f"  {prefix}session_id    : {result.session_id}")
    print(f"  {prefix}status_code   : {result.status_code!r}")
    print(f"  {prefix}source        : {result.source}")
    print(f"  {prefix}failure_class : {result.failure_class}")
    print(f"  {prefix}confidence    : {result.confidence}")
    print(f"  {prefix}method        : {result.method}")
    print(f"  {prefix}description   : {result.description}")
    print(f"  {prefix}is_auto_actionable : {result.is_auto_actionable}")
    print(f"  {prefix}discount_allowed   : {result.discount_allowed}")
    if result.llm_raw:
        print(f"  {prefix}llm_raw       : {result.llm_raw}")


def run_tests():
    use_mock_llm = os.environ.get("MOCK_LLM", "true").lower() != "false"
    llm_client = MockLLMClient() if use_mock_llm else None
    llm_label = "MOCK" if use_mock_llm else "REAL"

    print("=" * 70)
    print(f"Payment Failure Classifier — test run  [LLM={llm_label}]")
    print("=" * 70)

    failures = []

    # ---- Rule cases (no error_source — original behaviour must be unchanged) ----
    print(f"\n{'─'*70}")
    print(f"  RULE PATH ({len(RULE_CASES)} cases — all must be method=rule, confidence=1.0)")
    print(f"{'─'*70}")

    for idx, (status_code, exp_source, exp_class) in enumerate(RULE_CASES, 1):
        session = FakeSession(f"evt_rule_{idx:02d}", status_code, cart_value=1500.0)
        result = classify_payment_failure(session, llm_client=llm_client)

        ok = (
            result.method == "rule"
            and result.confidence == 1.0
            and result.source == exp_source
            and result.failure_class == exp_class
            and result.discount_allowed is False
        )
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"\n[{idx}] {status}  status_code={status_code!r}")
        print_result(result, prefix="  ")

        if not ok:
            failures.append(
                f"Rule case {status_code!r}: "
                f"expected source={exp_source!r} class={exp_class!r} method=rule conf=1.0, "
                f"got source={result.source!r} class={result.failure_class!r} "
                f"method={result.method!r} conf={result.confidence}"
            )

    # ---- Tier-2: error_source-only classification (no rule match) ----
    print(f"\n{'─'*70}")
    print(f"  TIER-2: SOURCE-ONLY PATH ({len(ERROR_SOURCE_ONLY_CASES)} cases — method=rule, confidence=0.9, NO LLM)")
    print(f"{'─'*70}")

    for idx, (status_code, error_source, exp_class, exp_source) in enumerate(ERROR_SOURCE_ONLY_CASES, 1):
        session = FakeSession(f"evt_src_{idx:02d}", status_code, cart_value=1500.0)
        result = classify_payment_failure(session, llm_client=llm_client, error_source=error_source)

        ok = (
            result.method == "rule"
            and result.confidence == 0.9
            and result.source == exp_source
            and result.failure_class == exp_class
            and result.discount_allowed is False
        )
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"\n[{idx}] {status}  status_code={status_code!r}  error_source={error_source!r}")
        print_result(result, prefix="  ")

        if not ok:
            failures.append(
                f"Source-only case status_code={status_code!r} source={error_source!r}: "
                f"expected class={exp_class!r} method=rule conf=0.9, "
                f"got class={result.failure_class!r} method={result.method!r} conf={result.confidence}"
            )

    # ---- Tier-1 override: rule hit but error_source contradicts rule ----
    print(f"\n{'─'*70}")
    print(f"  TIER-1 CONFLICT: SOURCE OVERRIDES RULE ({len(RULE_OVERRIDE_CASES)} cases — Razorpay source WINS)")
    print(f"{'─'*70}")

    for idx, (status_code, error_source, exp_class, exp_source) in enumerate(RULE_OVERRIDE_CASES, 1):
        session = FakeSession(f"evt_ovr_{idx:02d}", status_code, cart_value=1500.0)
        result = classify_payment_failure(session, llm_client=llm_client, error_source=error_source)

        ok = (
            result.method == "rule"
            and result.confidence == 1.0
            and result.source == exp_source      # Razorpay's real source, NOT the rule's
            and result.failure_class == exp_class
            and result.discount_allowed is False
        )
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"\n[{idx}] {status}  status_code={status_code!r}  error_source={error_source!r}  (rule would say source={dict(PAYMENT_FAILURE_RULES.get(status_code, {})).get('source', '?')})")
        print_result(result, prefix="  ")

        if not ok:
            failures.append(
                f"Rule-override case status_code={status_code!r} error_source={error_source!r}: "
                f"expected class={exp_class!r} source={exp_source!r}, "
                f"got class={result.failure_class!r} source={result.source!r}"
            )

    # ---- LLM fallback cases (no rule, no error_source) ----
    print(f"\n{'─'*70}")
    print(f"  LLM FALLBACK PATH ({len(LLM_FALLBACK_CASES)} cases — method=llm, conf<1.0)")
    print(f"{'─'*70}")

    for idx, (status_code, note) in enumerate(LLM_FALLBACK_CASES, 1):
        session = FakeSession(f"evt_llm_{idx:02d}", status_code, cart_value=2500.0)
        result = classify_payment_failure(session, llm_client=llm_client)

        ok = (
            result.method == "llm"
            and result.confidence < 1.0
            and result.discount_allowed is False
        )
        status = "✅ PASS" if ok else "❌ FAIL"
        print(f"\n[{idx}] {status}  status_code={status_code!r}  ({note})")
        print_result(result, prefix="  ")

        if not ok:
            failures.append(
                f"LLM fallback case {status_code!r}: "
                f"expected method=llm conf<1.0, "
                f"got method={result.method!r} conf={result.confidence}"
            )

    # ---- discount_allowed invariant across ALL cases ----
    print(f"\n{'─'*70}")
    print("  INVARIANT CHECK: discount_allowed is ALWAYS False for payment failures")
    print(f"{'─'*70}")
    all_codes = [c for (c, *_) in RULE_CASES] + [c for (c, *_) in LLM_FALLBACK_CASES]
    for code in all_codes:
        s = FakeSession(f"inv_{code}", code)
        r = classify_payment_failure(s, llm_client=llm_client)
        if r.discount_allowed:
            failures.append(f"INVARIANT BREACH: discount_allowed=True for {code!r}")
    print("  All status codes checked — discount_allowed=False invariant holds ✅")

    # ---- Summary ----
    total = len(RULE_CASES) + len(ERROR_SOURCE_ONLY_CASES) + len(RULE_OVERRIDE_CASES) + len(LLM_FALLBACK_CASES)
    print(f"\n{'='*70}")
    if failures:
        print(f"RESULT: {len(failures)} FAILURE(S)")
        for f in failures:
            print(f"  ❌ {f}")
        sys.exit(1)
    else:
        print(f"RESULT: ALL {total} CASES PASSED ✅")
    print("=" * 70)


if __name__ == "__main__":
    run_tests()

