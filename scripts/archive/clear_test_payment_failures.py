"""
Script: Clear Test Payment Failures for Merchant ID 3
=====================================================
Deletes all RecoveryCase rows with scenario=PAYMENT_FAILURE and their
associated RecoveryActionLog rows, for merchant_id=3 only.
Prints deleted counts before committing.
"""

import os
import sys

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal
from app.db_models import RecoveryCase, RecoveryActionLog, RecoveryScenario


def clear_test_payment_failures(target_merchant_id: int = 3):
    db = SessionLocal()
    try:
        # 1. Query target RecoveryCase rows for merchant_id=3 with scenario=PAYMENT_FAILURE only
        target_cases = db.query(RecoveryCase).filter(
            RecoveryCase.merchant_id == target_merchant_id,
            RecoveryCase.scenario == RecoveryScenario.PAYMENT_FAILURE
        ).all()

        case_ids = [c.id for c in target_cases]
        cases_count = len(case_ids)

        if not case_ids:
            print(f"No PAYMENT_FAILURE cases found for merchant_id={target_merchant_id}.")
            print("Deleted RecoveryCase rows: 0")
            print("Deleted RecoveryActionLog rows: 0")
            return 0, 0

        # 2. Query associated RecoveryActionLog rows
        target_logs = db.query(RecoveryActionLog).filter(
            RecoveryActionLog.case_id.in_(case_ids)
        ).all()
        logs_count = len(target_logs)

        # 3. Print counts before deletion/commit
        print(f"Target merchant_id: {target_merchant_id}")
        print(f"Found {cases_count} RecoveryCase row(s) with scenario=PAYMENT_FAILURE: {case_ids}")
        print(f"Found {logs_count} associated RecoveryActionLog row(s)")

        # 4. Perform deletions (logs first, then cases)
        if target_logs:
            for log in target_logs:
                db.delete(log)

        for case in target_cases:
            db.delete(case)

        # 5. Commit transaction
        db.commit()

        print(f"Successfully deleted {logs_count} RecoveryActionLog row(s).")
        print(f"Successfully deleted {cases_count} RecoveryCase row(s).")

        return cases_count, logs_count

    except Exception as e:
        db.rollback()
        print(f"Error occurred during cleanup: {e}", file=sys.stderr)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    clear_test_payment_failures(target_merchant_id=3)
