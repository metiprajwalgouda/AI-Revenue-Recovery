"""
SQLite database setup. Using SQLite (not Postgres/MySQL) deliberately -- it's a
single file, needs zero setup/server, and is genuinely fine for a buildathon demo's
data volume. Swapping to Postgres later would only mean changing DATABASE_URL.
"""

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.db_models import Base

DATABASE_URL = "sqlite:///./data/storefront.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _add_column_if_missing(table: str, column: str, coltype: str) -> None:
    """SQLite has no CREATE TABLE IF NOT EXISTS for new columns on an already-created
    table. create_all() will not ALTER existing rows, so demo DBs need this."""
    with engine.begin() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        existing = {row[1] for row in rows}
        if column not in existing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))


def init_db():
    """Creates all tables if they don't already exist. Safe to call on every startup."""
    import os
    os.makedirs("data", exist_ok=True)
    Base.metadata.create_all(bind=engine)
    _add_column_if_missing("checkout_sessions", "razorpay_order_id", "VARCHAR(100)")
    _add_column_if_missing("checkout_sessions", "is_high_priority", "BOOLEAN DEFAULT 0")
    _add_column_if_missing("checkout_sessions", "recovered_from_session_id", "INTEGER")
    _add_column_if_missing("recovery_outcomes", "delivery_status", "VARCHAR(40)")
    _add_column_if_missing("recovery_outcomes", "resume_url", "VARCHAR(500)")
    _add_column_if_missing("customer_users", "opted_out_of_marketing", "BOOLEAN DEFAULT 0")
    
    _add_column_if_missing("merchant_users", "contact_email", "VARCHAR(200)")
    _add_column_if_missing("merchant_users", "phone", "VARCHAR(20)")
    _add_column_if_missing("merchant_users", "business_category", "VARCHAR(100)")
    _add_column_if_missing("merchant_users", "logo_url", "VARCHAR(500)")
    _add_column_if_missing("merchant_users", "min_discount_pct", "INTEGER DEFAULT 5")
    _add_column_if_missing("merchant_users", "max_discount_pct", "INTEGER DEFAULT 20")
    _add_column_if_missing("merchant_users", "high_value_threshold_amount", "FLOAT DEFAULT 3000.0")
    _add_column_if_missing("merchant_users", "max_recovery_attempts", "INTEGER DEFAULT 3")
    _add_column_if_missing("merchant_users", "auto_call_high_priority", "BOOLEAN DEFAULT 0")
    _add_column_if_missing("recovery_cases", "coupon_code_used", "VARCHAR(50)")
    _add_column_if_missing("recovery_cases", "discount_amount", "FLOAT DEFAULT 0.0")

    # invoice_id migration note (added when Invoice model was introduced):
    #
    # FRESH DB  — create_all() above creates both the new `invoices` table AND
    #             recovery_cases.invoice_id as a proper FK column.  Correct.
    #
    # EXISTING DEV DB — The `invoices` table did not exist before; create_all()
    #             will CREATE it on startup (create_all skips existing tables, but
    #             creates missing ones).  Correct.
    #             HOWEVER: recovery_cases.invoice_id already exists as a plain
    #             INTEGER column with no FK constraint.  SQLite does not support
    #             ALTER COLUMN, so _add_column_if_missing cannot retrofit the FK.
    #             The FK is therefore advisory at the SQLAlchemy ORM layer on
    #             existing rows (SQLAlchemy checks model metadata, not PRAGMA
    #             foreign_keys which defaults to OFF on SQLite anyway).
    #             Practical impact: all existing rows have invoice_id = NULL, so
    #             there are no referential integrity violations.  New OVERDUE_
    #             RECEIVABLE rows will be inserted with a valid invoices.id by
    #             the application layer.
    #
    # RECOMMENDED: For a clean dev environment, delete data/storefront.db and
    #             let create_all() rebuild from scratch.  The FK will be properly
    #             enforced at the column level on the new schema.



def get_db():
    """FastAPI dependency -- yields a session, guarantees it's closed after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()