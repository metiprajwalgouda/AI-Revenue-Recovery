"""
One-off backfill script: Re-classify historical RecoveryCase rows where
scenario=CHECKOUT_ABANDONMENT and classification="unknown".

Uses the now-fixed Gemini-3.6-flash classifier pipeline, reconstructs
the original session signals, updates classification metadata on RecoveryCase,
and appends a transparent audit log entry to RecoveryActionLog.
"""

import os
import sys
import json
import uuid
from pathlib import Path
from datetime import datetime, timezone

# Ensure utf-8 output on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
load_dotenv(project_root / ".env", override=True)

from app.db import SessionLocal
from app.db_models import (
    RecoveryCase, RecoveryActionLog, RecoveryScenario,
    ClassificationMethod, CheckoutSession
)
from app.models import CheckoutEvent
from app.agent.classifier import classify
from app.config import GEMINI_DEFAULT_MODEL


def run_backfill():
    db = SessionLocal()
    now = datetime.now(timezone.utc)

    try:
        # Find cases where scenario is CHECKOUT_ABANDONMENT and classification is unknown / None
        cases = (
            db.query(RecoveryCase)
            .filter(
                RecoveryCase.scenario == RecoveryScenario.CHECKOUT_ABANDONMENT,
                (RecoveryCase.classification == "unknown") |
                (RecoveryCase.classification == None) |
                (RecoveryCase.classification == "unclassified")
            )
            .order_by(RecoveryCase.id.asc())
            .all()
        )

        print("=" * 110)
        print(f"STARTING RECLASSIFICATION BACKFILL FOR {len(cases)} CHECKOUT ABANDONMENT CASES")
        print(f"LLM Model: {GEMINI_DEFAULT_MODEL}")
        print("=" * 110)

        if not cases:
            print("No cases found matching scenario=CHECKOUT_ABANDONMENT and classification='unknown'.")
            return []

        results_table = []

        for idx, case in enumerate(cases, 1):
            old_class = case.classification or "unknown"
            old_conf = case.confidence if case.confidence is not None else 0.0

            # Reconstruct session signals
            session = case.checkout_session
            if not session and case.checkout_session_id:
                session = db.query(CheckoutSession).filter(CheckoutSession.id == case.checkout_session_id).first()

            cart_value = case.amount_at_risk or (session.cart_value if session else 0.0)
            status_code = session.payment_status_code if session else None
            page_load_time = session.page_load_time_ms if session else None
            time_on_page = session.time_on_checkout_page_sec if session else None
            customer_email = (session.customer_email if session else None) or (case.customer.email if case.customer else "guest@example.com")
            customer_phone = (session.customer_phone if session else None) or (case.customer.phone if case.customer else "+919999999999")
            customer_name = (session.customer_name if session else None) or (case.customer.name if case.customer else "Customer")

            # Construct notes / context for LLM
            notes = (
                f"Customer {customer_name} ({customer_email}) abandoned cart with total value Rs. {cart_value:.2f}. "
                f"Time spent on checkout: {time_on_page if time_on_page is not None else 'unknown'}s. "
                f"Page load time: {page_load_time if page_load_time is not None else 'normal'}ms. "
                f"Payment status: {status_code if status_code else 'No payment attempted'}."
            )

            event = CheckoutEvent(
                event_id=session.event_id if session else f"case_{case.id}",
                customer_id=str(case.customer_user_id or "guest"),
                customer_email=customer_email,
                customer_phone=customer_phone,
                cart_value=cart_value,
                checkout_started_at=session.started_at if session and session.started_at else (case.created_at or now),
                abandoned_at=session.abandoned_at if session and session.abandoned_at else (case.created_at or now),
                payment_method_attempted=None,
                payment_status_code=status_code,
                page_load_time_ms=page_load_time,
                otp_requested=False,
                otp_verified=False,
                time_on_checkout_page_sec=time_on_page,
                notes=notes,
            )

            # Classify through the fixed pipeline with retry on rate limit
            import time
            result = None
            for retries in range(3):
                result = classify(event)
                new_reason_val = result.predicted_reason.value if hasattr(result.predicted_reason, "value") else str(result.predicted_reason)
                if new_reason_val != "unknown" or result.confidence > 0.0:
                    break
                print(f"  [Retry {retries+1}/3] Received unknown/low confidence, pausing 6s before retry...")
                time.sleep(6.0)

            new_reason_val = result.predicted_reason.value if hasattr(result.predicted_reason, "value") else str(result.predicted_reason)

            # 1. Update RecoveryCase metadata (without altering financial or lifecycle status)
            case.classification = new_reason_val
            case.classification_source = ClassificationMethod.LLM if result.method_used == "llm" else ClassificationMethod.RULE
            case.confidence = float(result.confidence)

            # 2. Append transparent audit log entry to RecoveryActionLog
            ladder_step = case.ladder_step or 0
            idempotency_key = f"{case.id}:{ladder_step}:reclassification_backfill_{uuid.uuid4().hex[:6]}"
            
            action_log = RecoveryActionLog(
                case_id=case.id,
                idempotency_key=idempotency_key,
                ladder_step=ladder_step,
                action_type="reclassification_backfill",
                reason=f"LLM Backfill Diagnosis ({new_reason_val}, conf={result.confidence:.2f}): {result.reasoning}",
                guardrail_checks=json.dumps({
                    "channel": "llm_backfill",
                    "model": GEMINI_DEFAULT_MODEL,
                    "confidence": result.confidence,
                    "method": result.method_used,
                    "previous_classification": old_class
                }),
                outcome="sent",
                amount_offered=None,
                coupon_code=None,
                requires_human_approval=False,
                approved_by=case.merchant_id,
                approved_at=now,
            )
            db.add(action_log)

            results_table.append({
                "case_id": case.id,
                "cart_value": cart_value,
                "old_class": old_class,
                "old_conf": old_conf,
                "new_class": new_reason_val,
                "new_conf": result.confidence,
                "method": result.method_used,
                "reasoning": result.reasoning
            })

            print(f"[{idx}/{len(cases)}] Case #{case.id} (Rs. {cart_value:.2f}): {old_class} -> {new_reason_val} (conf: {result.confidence:.2f}, via {result.method_used})")
            
            time.sleep(3.5)

        db.commit()
        print("\n" + "=" * 110)
        print("BACKFILL COMPLETED AND COMMITTED SUCCESSFULLY!")
        print("=" * 110)

        # Print detailed Before/After comparison table
        print(f"\n{'Case ID':<8} | {'Cart (Rs)':<10} | {'Old Class':<12} | {'New Class':<24} | {'Conf':<6} | {'Method':<6} | {'New Reasoning'}")
        print("-" * 120)
        for r in results_table:
            print(f"#{r['case_id']:<7} | {r['cart_value']:<10.2f} | {r['old_class']:<12} | {r['new_class']:<24} | {r['new_conf']:<6.2f} | {r['method']:<6} | {r['reasoning']}")

        return results_table

    except Exception as e:
        db.rollback()
        print(f"\nERROR during backfill: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    run_backfill()
