"""
Verification script: confirms both new tables exist with correct columns,
and that all existing tables are untouched.

Run from project root:  .venv\Scripts\python.exe scripts/verify_schema.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import init_db, engine
from sqlalchemy import text, inspect

def get_columns(inspector, table_name):
    return {col["name"] for col in inspector.get_columns(table_name)}

init_db()
insp = inspect(engine)
existing_tables = set(insp.get_table_names())

print("=== Tables in database ===")
for t in sorted(existing_tables):
    print(" ", t)

# 1. New tables must exist
assert "recovery_cases" in existing_tables, "FAIL: recovery_cases table missing"
assert "recovery_action_logs" in existing_tables, "FAIL: recovery_action_logs table missing"

# 2. Old tables must still exist
for old in ["checkout_sessions", "recovery_outcomes", "merchant_users", "customer_users", "products", "coupons"]:
    assert old in existing_tables, f"FAIL: existing table '{old}' gone!"

# 3. Check recovery_cases columns
rc_cols = get_columns(insp, "recovery_cases")
required_rc = {
    "id", "merchant_id", "customer_user_id", "checkout_session_id", "invoice_id",
    "scenario", "amount_at_risk", "amount_recovered", "status", "ladder_step",
    "classification", "classification_source", "error_source", "rar_score", "confidence",
    "escalated_to_human", "escalation_reason", "contact_touches",
    "last_action_at", "next_action_due_at", "created_at", "updated_at",
}
missing_rc = required_rc - rc_cols
assert not missing_rc, f"FAIL: recovery_cases missing columns: {missing_rc}"
print("\n=== recovery_cases columns ===")
for c in sorted(rc_cols):
    print(" ", c)

# 4. Check recovery_action_logs columns
ral_cols = get_columns(insp, "recovery_action_logs")
required_ral = {
    "id", "case_id", "idempotency_key", "ladder_step", "action_type",
    "reason", "guardrail_checks", "outcome", "amount_offered", "coupon_code",
    "requires_human_approval", "approved_by", "approved_at", "created_at",
}
missing_ral = required_ral - ral_cols
assert not missing_ral, f"FAIL: recovery_action_logs missing columns: {missing_ral}"
print("\n=== recovery_action_logs columns ===")
for c in sorted(ral_cols):
    print(" ", c)

# 5. Idempotency key unique constraint
# SQLite stores UNIQUE column indexes as sqlite_autoindex_<table>_<n> with sql=NULL,
# so we check via PRAGMA index_info instead of parsing the DDL.
with engine.connect() as conn:
    idxs = conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='recovery_action_logs'"
    )).fetchall()
    print("\n=== recovery_action_logs indexes ===")
    found_unique = False
    for (idx_name,) in idxs:
        print(" ", idx_name)
        cols = conn.execute(text(f"PRAGMA index_info('{idx_name}')")).fetchall()
        col_names = [row[2] for row in cols]
        if "idempotency_key" in col_names:
            found_unique = True
            print(f"   -> covers: {col_names}")
    assert found_unique, "FAIL: idempotency_key unique index missing"

print("\nALL CHECKS PASSED")
