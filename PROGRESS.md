# PROGRESS.md — Checkout Recovery Agent

## Current Status: Step 1 (audit) complete. Step 2 schema migration complete.

---

## Change Log

### Entry 1 — Initialized
- **Date:** 2026-09-03
- **What changed:** Initialized this file.
- **Files touched:** `PROGRESS.md`
- **Tested:** N/A
- **Pending:** All steps per user request.

---

### Entry 2 — Unified Recovery-Case Schema Added (schema migration)
- **Date:** 2026-09-04
- **What changed:** Added three new enums and two new SQLAlchemy model classes to
  `app/db_models.py`. No existing models touched.
  - `RecoveryScenario` (PAYMENT_FAILURE, CHECKOUT_ABANDONMENT, OVERDUE_RECEIVABLE)
  - `CaseStatus` (NEW, AT_RISK, INTERVENING, RECOVERED, LOST, ESCALATED)
  - `ClassificationMethod` (RULE, LLM)
  - `RecoveryCase` → table: `recovery_cases`
  - `RecoveryActionLog` → table: `recovery_action_logs`

- **Migration tool:** Project uses no Alembic. Migration strategy is SQLAlchemy
  `Base.metadata.create_all()` (runs on every app startup) for new tables, plus
  `_add_column_if_missing()` in `db.py` for columns added to existing tables.
  Both new tables are 100% new (`CREATE TABLE IF NOT EXISTS` is handled by SQLAlchemy
  `create_all` automatically), so no `_add_column_if_missing` calls were needed.

- **Files touched:** `app/db_models.py` (appended lines 217-380)
- **`db.py` touched:** No — `create_all()` on startup auto-creates new tables.

- **Tested:**
  - `python -c "from app.db_models import RecoveryCase, RecoveryActionLog, ..."` → exit 0, all enums and models imported without error.
  - `init_db()` ran → "init_db OK" confirmed before any subsequent error in test scaffolding.
  - Old models (`CheckoutSession`, `RecoveryOutcomeRecord`) confirmed still present.

- **Known: table existence check script had a quoting issue in the one-liner** — resolved
  by creating `/scripts/verify_schema.py` which passed all 5 assertions cleanly.

- **Tested (confirmed with `.venv_new`):**
  1. `scripts/verify_schema.py` → ALL CHECKS PASSED
     - Tables: `recovery_cases`, `recovery_action_logs` created alongside all old tables
     - `recovery_cases`: all 22 columns present
     - `recovery_action_logs`: all 14 columns present
     - `idempotency_key` unique index confirmed via PRAGMA index_info
  2. Abandonment pipeline smoke test:
     - `classify(gateway_timeout)` → `network_drop` (rule, conf=0.9) ✅
     - `decide_action()` → `send_payment_link` ✅
     - `is_discount_allowed('network_drop')` → False ✅
     - `is_discount_allowed('high_amount_hesitation')` → True ✅
     - `is_discount_allowed('card_declined')` → False ✅
     - `classify(cart=25000, dwell=200s)` → `high_amount_hesitation` ✅
     - `decide_action()` → `send_discount_nudge` ✅

- **Active venv:** `.venv_new` (`.venv` has a broken pydantic_core binary on this machine)

- **Pending:**
  - Build Step 2 Revenue-at-Risk dashboard (await user approval to proceed).

---

### Entry 3 — Payment Failure Classifier Added
- **Date:** 2026-09-04
- **What changed:** New file `app/agent/payment_failure_classifier.py`. No existing files touched.
  - `PAYMENT_FAILURE_RULES` — 8-entry dict mapping known `payment_status_code` values to
    `{source, class, description}`. Covers all Razorpay / bank error codes from the spec.
  - `PaymentFailureClassification` — typed dataclass (separate from `ClassificationResult`
    to prevent accidental interchange with the abandonment classifier output).
    Properties: `is_auto_actionable` (False for `risk_terminal`),
    `discount_allowed` (always False — payment failures never receive discounts).
  - `classify_payment_failure(session, llm_client=None)` — rule-first (conf=1.0),
    LLM fallback with payment-failure-specific system prompt (scenario-aware).
  - `_llm_classify_payment_failure()` — internal LLM path, shares Gemini/Anthropic
    plumbing with the abandonment classifier but uses a separate system prompt and
    a different output vocabulary (`source` + `class` instead of `predicted_reason`).
  - `anthropic` import is deferred inside the `if llm_client is None:` branch so
    tests can pass a mock client without touching the SDK at all.

- **Files touched:** `app/agent/payment_failure_classifier.py` [NEW]

- **Test:** `scripts/test_payment_failure_classifier.py` [NEW]
  Run: `.venv_new\Scripts\python.exe scripts\test_payment_failure_classifier.py`
  Result: ALL 11 CASES PASSED (exit 0, zero stderr)
    - 8 rule-path cases: all method=rule, confidence=1.0, correct source+class
    - 3 LLM-fallback cases: method=llm, confidence<1.0, mock returns source=unknown
    - discount_allowed=False invariant verified across all 11 status codes

- **Active venv:** `.venv_new`

- **Pending:**
  - Wire `classify_payment_failure` into `RecoveryCase` creation (Step 2 dashboard).
  - Build Revenue-at-Risk dashboard UI.

---

### Entry 4 — Payment Failure Decision + Action Ladder Wired
- **Date:** 2026-09-04
- **What changed:**

  **New file: `app/agent/payment_failure_actions.py`**
  - `run_payment_failure_recovery(session, classification, db, razorpay_client)` — entry point
  - Creates/updates `RecoveryCase`, appends `RecoveryActionLog` rows.
  - Four decision branches:
    1. `risk_terminal` OR `confidence < 0.5` → `ESCALATED`, `human_escalation` logged, no comms
    2. `needs_customer_action` → `INTERVENING`, `ux_suppressed` logged, no comms
    3. `needs_alternate_method` / `retryable_technical` → circuit-breaker check,
       Razorpay payment link created at exact cart value (NO discount), sent,
       `contact_touches` incremented
    4. Circuit breaker (contact_touches ≥ 3) → `LOST`
  - Discount guard: if `discount_allowed=True` somehow reaches this layer,
    blocks with `pending_approval` (defence-in-depth).
  - `_get_or_create_case` is idempotent — safe to call on retries.
  - `_write_action_log` uses `idempotency_key` dedup via IntegrityError catch.
  - `_send_payment_link_notification` fires in-app + email, non-fatal on failure.

  **Modified: `app/main.py`**
  - `CheckoutAbandonRequest` — new optional `payment_status_code` field.
  - `abandon_checkout` endpoint — routes to `payment_failure` pipeline when
    `payment_status_code` is present; otherwise runs existing abandonment pipeline
    **byte-for-byte unchanged**.

  **Modified: `app/templates/cart.html`**
  - Added `"payment.failed"` handler to Razorpay options object: forwards
    `response.error.reason` (the machine-readable error code) as `payment_status_code`.
  - `ondismiss` handler updated with a `_paymentFailedFired` flag so the trailing
    `ondismiss` after a `payment.failed` doesn't send a duplicate `/abandon` request.

- **Files changed:**
  - `app/agent/payment_failure_actions.py` [NEW]
  - `app/main.py` (CheckoutAbandonRequest + abandon_checkout routing)
  - `app/templates/cart.html` (payment.failed handler)

- **Tests: `scripts/test_payment_failure_actions.py`** [NEW]
  Run: `.venv_new\Scripts\python.exe scripts\test_payment_failure_actions.py`
  Result: ALL 8 SCENARIOS PASSED ✅ (22 assertions, exit 0)
    1. risk_terminal → ESCALATED, contact_touches=0 ✅
    2. low_confidence → ESCALATED, reason=low_confidence ✅
    3. needs_customer_action → INTERVENING, ux_suppressed, contact_touches=0 ✅
    4. needs_alternate_method → INTERVENING, payment_link_sent, amount=cart_value, no coupon ✅
    5. retryable_technical → INTERVENING, payment_link_sent ✅
    6. circuit breaker (3 touches) → LOST ✅
    7. idempotency: re-running on terminal case, only 1 action log ✅
    8. Abandonment pipeline unchanged: is_discount_allowed, cap_discount intact ✅

  Stderr in tests: WS push fails (no event loop in test) + Resend email fails
  (no API key for test address). Both caught by non-fatal try/except, case state
  unaffected.

---

### Entry 5 — Idempotency and Scenario Tagging Fixes
- **Date:** 2026-09-04
- **What changed:**
  - **`app/live_recovery.py`**: Added `_upsert_abandonment_recovery_case()` inside the abandonment pipeline. Genuine abandonments now write a `RecoveryCase` row with `scenario=CHECKOUT_ABANDONMENT` in the same transaction as the original `RecoveryOutcomeRecord`. This ensures both abandonment and payment-failure data live in the same table for the Step 2 dashboard, preventing skewed metrics.
  - **`app/agent/payment_failure_actions.py`**: Fixed idempotency ordering to prevent double-emailing on concurrent duplicate webhooks. Removed the flawed `pre_send_idempotency_key` read-before-write check (which inadvertently broke the circuit breaker by confusing legitimate subsequent failures with retries). Now explicitly checks the return value of `_write_action_log` (which relies on an atomic DB `IntegrityError`) and short-circuits *before* calling `_send_payment_link_notification` if the log row is a duplicate.
  - **`scripts/test_payment_failure_actions.py`**: Fixed a stale ORM state bug in `_get_or_create_case` (`db.expire(existing)`) that prevented the circuit breaker test from seeing consecutive touch increments across the same test session.

- **Files touched:**
  - `app/live_recovery.py`
  - `app/agent/payment_failure_actions.py`
  - `scripts/test_payment_failure_actions.py`

- **Pending:**
  - Build Revenue-at-Risk dashboard UI (Step 2).

---

### Entry 6 — Payment Failure Tab Added to Merchant Dashboard
- **Date:** 2026-09-04
- **What changed:**
  - **`app/templates/merchant_payment_failures.html` [NEW]**:
    - Top summary row with 5 KPI cards: Total At Risk, Total Recovered, Recovery Rate %, Escalated to Human, Circuit Breaker Trips (&ge;3 touches).
    - Status & Error Source filters.
    - Case table: Customer, Amount (at-risk & recovered), Classification with colored badge for error_source (`bank` [red], `gateway` [amber], `customer` [blue], `business` [purple], `unknown` [gray]), Ladder step, Status badge, Last action time, and "View audit trail" button.
    - Case detail modal: chronological list of `RecoveryActionLog` records showing timestamp, ladder step, action type, verbatim `reason` box, parsed `guardrail_checks` as checkmarks, and `outcome` badge.
    - Added "Approve" / "Reject" buttons for logs with `outcome="pending_approval"`.
  - **`app/merchant_extensions.py`**:
    - Added `GET /api/merchant/payment-failures` returning aggregated KPIs and cases.
    - Added `GET /api/merchant/payment-failures/{case_id}/audit-trail` returning chronological action logs with parsed guardrail checks.
    - Added `POST /api/merchant/recovery-actions/{log_id}/approve` to approve pending actions.
    - Added `POST /api/merchant/recovery-actions/{log_id}/reject` to reject pending actions.
  - **`app/main.py`**:
    - Added `@app.get("/merchant/payment-failures")` page route serving `merchant_payment_failures.html`.
    - Removed top-level `import anthropic` and deferred it in `get_llm_client()` to prevent startup crashes when Anthropic beta modules are unavailable.
  - **`app/templates/base_merchant.html`**:
    - Added sidebar navigation link for "Payment Failures".

- **Files touched:**
  - `app/templates/merchant_payment_failures.html` [NEW]
  - `app/merchant_extensions.py`
  - `app/main.py`
  - `app/templates/base_merchant.html`
  - `scripts/test_merchant_payment_failures_dashboard.py` [NEW]

- **Tests:**
  - `scripts/test_merchant_payment_failures_dashboard.py` (ALL 21 CHECKS PASSED ✅)
  - `scripts/verify_schema.py` (ALL CHECKS PASSED ✅)
  - `scripts/test_payment_failure_classifier.py` (ALL 11 CASES PASSED ✅)
  - `scripts/test_payment_failure_actions.py` (ALL 8 SCENARIOS PASSED ✅)

- **Pending:**
  - Propose/finalize Invoice model schema for `OVERDUE_RECEIVABLE`.
  - Build the comprehensive 3-scenario Revenue-at-Risk dashboard (Step 2).

---

### Entry 7 — Priority Escalation Queue Extended Across All Recovery Scenarios
- **Date:** 2026-09-04
- **What changed:**
  - **`app/main.py`**:
    - Extended `GET /api/merchant/priority` to query and return `RecoveryCase` rows where `escalated_to_human=True` across ALL recovery scenarios (`payment_failure`, `checkout_abandonment`, `overdue_receivable`), combined seamlessly with high-priority abandoned `CheckoutSession`s.
    - Updated `POST /api/merchant/priority/{session_id}/call` and `POST /api/merchant/priority/{session_id}/contact` to accept either a `CheckoutSession.id` or a standalone `RecoveryCase.id`, resolving escalations on contact and tracking outreach touches.
  - **`app/templates/merchant_priority.html`**:
    - Added a **Scenario** column with colored badges (`Payment Failure` [red], `Abandonment` [amber], `Receivable` [purple]).
    - Added a **Scenario Filter** dropdown (`All Scenarios`, `Payment Failure`, `Checkout Abandonment`, `Overdue Receivable`).
    - Added Reason and Escalation details display.
    - Preserved all existing manual outreach actions ("Send Offer", "Call", "Mark Contacted").

- **Files touched:**
  - `app/main.py`
  - `app/templates/merchant_priority.html`
  - `scripts/test_priority_queue_all_scenarios.py` [NEW]

- **Tests:**
  - `scripts/test_priority_queue_all_scenarios.py` (ALL 14 CHECKS PASSED ✅)
  - `scripts/test_merchant_payment_failures_dashboard.py` (ALL 21 CHECKS PASSED ✅)
  - `scripts/test_payment_failure_actions.py` (ALL 8 SCENARIOS PASSED ✅)

- **Pending:**
  - Propose/finalize Invoice model schema for `OVERDUE_RECEIVABLE`.
  - Build the comprehensive 3-scenario Revenue-at-Risk dashboard (Step 2).

---

### Entry 8 — Audit Logging for Manual Offers & ID Collision Elimination
- **Date:** 2026-09-04
- **What changed:**
  - **`app/merchant_extensions.py`**:
    - Updated `manual_recovery` (`/api/merchant/manual-recovery` / "Send Offer") to write a `RecoveryActionLog` row for the associated `RecoveryCase` whenever a manual offer is sent.
    - Sets `requires_human_approval=False`, `approved_by=current_merchant.id`, `outcome="sent"`, `reason` with custom merchant message, and sets `guardrail_checks` with discount and channel metadata.
    - Sets `case.escalated_to_human=False` and increments `case.contact_touches`.
  - **`app/main.py` & `app/templates/merchant_priority.html`**:
    - Resolved table ID collisions in priority endpoints (`/call` and `/contact`) by introducing prefix-based target identifiers (`case_123` vs `session_123`).
    - Added helper `_resolve_priority_target(target_id, db)` that deterministically routes actions to `RecoveryCase` vs `CheckoutSession`, preventing accidental modification of matching auto-increment IDs.
    - `mark_priority_contacted` now also logs a `manual_contact` entry into `RecoveryActionLog`.

- **Files touched:**
  - `app/merchant_extensions.py`
  - `app/main.py`
  - `app/templates/base_merchant.html`
  - `app/templates/merchant_priority.html`
  - `scripts/test_priority_queue_all_scenarios.py`

- **Tests:**
  - `scripts/test_priority_queue_all_scenarios.py` (ALL 23 CHECKS PASSED ✅)
  - `scripts/test_merchant_payment_failures_dashboard.py` (ALL 21 CHECKS PASSED ✅)

---

### Entry 9 — Advanced Metrics Extended with Scenario Breakdown
- **Date:** 2026-09-04
- **What changed:**
  - **`app/merchant_extensions.py`**:
    - Extended `GET /api/merchant/advanced-metrics` to compute stats from `recovery_cases` grouped by scenario:
      - `payment_failure`: `{at_risk, recovered, recovery_rate, escalated, circuit_breaker_trips}`
      - `checkout_abandonment`: `{at_risk, recovered, recovery_rate, escalated, circuit_breaker_trips}`
      - `overdue_receivable`: zeroed placeholder `{at_risk: 0.0, recovered: 0.0, recovery_rate: 0.0, escalated: 0, circuit_breaker_trips: 0}`
      - `overall`: `{cost_per_rupee_recovered, total_at_risk, total_recovered}`
    - Maintained full backward compatibility for existing dashboard keys (`funnel`, `financials`, `cohorts`, `audit_logs`).

- **Files touched:**
  - `app/merchant_extensions.py`
  - `scripts/test_advanced_metrics_scenarios.py` [NEW]

- **Tests:**
  - `scripts/test_advanced_metrics_scenarios.py` (ALL CHECKS PASSED ✅)
  - `scripts/test_priority_queue_all_scenarios.py` (ALL 23 CHECKS PASSED ✅)
  - `scripts/test_merchant_payment_failures_dashboard.py` (ALL 21 CHECKS PASSED ✅)

- **Pending:**
  - Propose/finalize Invoice model schema for `OVERDUE_RECEIVABLE`.
  - Build the comprehensive 3-scenario Revenue-at-Risk dashboard (Step 2).







