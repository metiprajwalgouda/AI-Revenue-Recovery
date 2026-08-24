"""
Database models for the real storefront.

Design decision: SQLite via SQLAlchemy, not the JSONL audit log from the batch
pipeline. Why the switch: a live site needs queries ("show me all abandoned
sessions from today", "sum recovered amount this week"), and JSONL is fine for
an append-only batch log but painful to query. The batch pipeline's audit_log.py
still exists and still works for the --mode simulated/live batch runs -- this is
a SEPARATE persistence layer for the live site specifically.

CheckoutSession is the live-site equivalent of the synthetic CheckoutEvent --
same underlying signals, but populated from REAL browser/customer activity
instead of the dataset generator.
"""

import enum
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Enum as SAEnum, ForeignKey, Text
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    price = Column(Float, nullable=False)  # in rupees
    stock = Column(Integer, nullable=False, default=0)
    image_url = Column(String(500), nullable=True)
    is_active = Column(Boolean, default=True)  # soft-delete instead of hard delete
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class SessionStatus(str, enum.Enum):
    STARTED = "started"        # customer landed on checkout, payment not yet attempted/completed
    COMPLETED = "completed"    # customer paid successfully
    ABANDONED = "abandoned"    # timed out without completing -- feeds the recovery agent
    RECOVERED = "recovered"    # was abandoned, agent's recovery action led to a confirmed payment


class CheckoutSession(Base):
    """Live-site equivalent of the synthetic CheckoutEvent -- but built from REAL
    browser signals instead of generated data. Some fields the synthetic dataset
    had (otp_requested, payment_status_code) may be genuinely unavailable from a
    real Razorpay Checkout.js integration -- left nullable on purpose, since the
    classifier already handles missing signals correctly (see classifier.py)."""

    __tablename__ = "checkout_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(64), unique=True, nullable=False)  # public-facing id, e.g. "chk_<uuid>"

    customer_name = Column(String(200), nullable=True)
    customer_email = Column(String(200), nullable=False)
    customer_phone = Column(String(20), nullable=False)

    cart_value = Column(Float, nullable=False)
    cart_json = Column(Text, nullable=True)  # serialized list of {product_id, qty, price} for display

    status = Column(SAEnum(SessionStatus), default=SessionStatus.STARTED, nullable=False)

    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)
    abandoned_at = Column(DateTime, nullable=True)

    # Real signals we CAN capture from a browser + Razorpay Checkout.js
    page_load_time_ms = Column(Integer, nullable=True)
    time_on_checkout_page_sec = Column(Integer, nullable=True)
    payment_status_code = Column(String(100), nullable=True)  # from Razorpay failure callback, if any
    razorpay_payment_id = Column(String(100), nullable=True)  # set only if actually paid

    opted_out_of_marketing = Column(Boolean, default=False)
    previous_recovery_attempts = Column(Integer, default=0)

    recovery_outcome = relationship("RecoveryOutcomeRecord", back_populates="session", uselist=False)


class RecoveryOutcomeRecord(Base):
    """Persisted version of the RecoveryOutcome pydantic model (models.py) --
    one row per checkout_session that went through the recovery pipeline."""

    __tablename__ = "recovery_outcomes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("checkout_sessions.id"), unique=True, nullable=False)

    predicted_reason = Column(String(50), nullable=False)
    confidence = Column(Float, nullable=False)
    classification_method = Column(String(10), nullable=False)  # "rule" or "llm"
    reasoning = Column(Text, nullable=True)

    action_taken = Column(String(50), nullable=False)
    action_success = Column(Boolean, nullable=False)
    amount_offered = Column(Float, nullable=True)
    confirmed_recovered_amount = Column(Float, nullable=True)
    payment_link_id = Column(String(100), nullable=True)
    payment_link_url = Column(String(500), nullable=True)
    error_message = Column(Text, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    reconciled_at = Column(DateTime, nullable=True)

    session = relationship("CheckoutSession", back_populates="recovery_outcome")