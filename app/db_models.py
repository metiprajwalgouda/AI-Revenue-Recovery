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
    Column, Integer, String, Float, Boolean, DateTime, Enum as SAEnum, ForeignKey, Text, func
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class MerchantUser(Base):
    """A store owner. Separate table from CustomerUser on purpose -- see auth.py's
    module docstring for why merchant and customer sessions are never interchangeable,
    even though both are 'a user who can log in'."""

    __tablename__ = "merchant_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(200), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    store_name = Column(String(200), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    # Merchant Profile & Settings
    contact_email = Column(String(200), nullable=True)
    phone = Column(String(20), nullable=True)
    business_category = Column(String(100), nullable=True)
    logo_url = Column(String(500), nullable=True)
    
    min_discount_pct = Column(Integer, default=5, nullable=False)
    max_discount_pct = Column(Integer, default=20, nullable=False)
    high_value_threshold_amount = Column(Float, default=3000.0, nullable=False)
    max_recovery_attempts = Column(Integer, default=3, nullable=False)
    auto_call_high_priority = Column(Boolean, default=False)
    auto_email_high_priority = Column(Boolean, default=False)
    automated_recovery_message = Column(Text, nullable=True)

    products = relationship("Product", back_populates="merchant")


class CustomerUser(Base):
    """A shopper with a real account (not a guest). Separate table from MerchantUser --
    a customer session token must never be usable to access merchant-only routes,
    and vice versa, even if someone tampers with a cookie value."""

    __tablename__ = "customer_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(200), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    name = Column(String(200), nullable=True)
    phone = Column(String(20), nullable=True)
    is_active = Column(Boolean, default=True)
    opted_out_of_marketing = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Coupon(Base):
    """Merchant-defined coupon codes."""
    __tablename__ = "coupons"

    id = Column(Integer, primary_key=True, autoincrement=True)
    merchant_id = Column(Integer, ForeignKey("merchant_users.id"), nullable=False)
    code = Column(String(50), unique=True, nullable=False, index=True)
    discount_pct = Column(Integer, nullable=True)
    discount_amount = Column(Float, nullable=True)
    active = Column(Boolean, default=True, nullable=False)
    usage_limit = Column(Integer, nullable=True)
    times_used = Column(Integer, default=0, nullable=False)


class CouponUsageLog(Base):
    """Log of which customers used which coupons."""
    __tablename__ = "coupon_usage_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    coupon_id = Column(Integer, ForeignKey("coupons.id"), nullable=False)
    customer_email = Column(String(100), nullable=True)
    used_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    order_value = Column(Float, nullable=False)
    discount_amount = Column(Float, nullable=False)
    
    coupon = relationship("Coupon", backref="usage_logs")

class InAppNotification(Base):
    """Real-time in-app notifications for customers."""
    __tablename__ = "in_app_notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    customer_user_id = Column(Integer, ForeignKey("customer_users.id"), nullable=False)
    title = Column(String(200), nullable=False)
    message = Column(Text, nullable=False)
    action_url = Column(String(500), nullable=True)
    is_read = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    
    customer = relationship("CustomerUser", backref="notifications")

class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    merchant_id = Column(Integer, ForeignKey("merchant_users.id"), nullable=False)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    price = Column(Float, nullable=False)  # in rupees
    stock = Column(Integer, nullable=False, default=0)
    image_url = Column(String(500), nullable=True)
    is_active = Column(Boolean, default=True)  # soft-delete instead of hard delete
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    merchant = relationship("MerchantUser", back_populates="products")


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
    customer_user_id = Column(Integer, ForeignKey("customer_users.id"), nullable=False)

    customer_name = Column(String(200), nullable=True)
    customer_email = Column(String(200), nullable=False)
    customer_phone = Column(String(20), nullable=False)

    cart_value = Column(Float, nullable=False)
    final_amount_charged = Column(Float, nullable=True)
    applied_coupon_id = Column(Integer, ForeignKey("coupons.id"), nullable=True)
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
    razorpay_order_id = Column(String(100), nullable=True)

    opted_out_of_marketing = Column(Boolean, default=False)
    previous_recovery_attempts = Column(Integer, default=0)
    is_high_priority = Column(Boolean, default=False, nullable=False)
    recovered_from_session_id = Column(Integer, ForeignKey("checkout_sessions.id"), nullable=True)
    is_control_group = Column(Boolean, default=False, server_default="0")

    recovery_outcome = relationship("RecoveryOutcomeRecord", back_populates="session", uselist=False)


class RecoveryOutcomeRecord(Base):
    """Persisted recovery attempt for a live (or batch-bridged) checkout session.

    This is the audit-trail row the buildathon criteria call a RecoveryAttempt:
    diagnosis, decision, action, and delivery outcome live here so analytics
    and the merchant orders UI read one shape instead of two tables.
    """

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
    # sent | failed | suppressed | skipped_no_key — how the live email step finished
    delivery_status = Column(String(40), nullable=True)
    resume_url = Column(String(500), nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    reconciled_at = Column(DateTime, nullable=True)

    session = relationship("CheckoutSession", back_populates="recovery_outcome")


# ---------------------------------------------------------------------------
# Unified Recovery-Case Schema
# Added alongside the existing RecoveryOutcomeRecord (which is NOT removed).
# These new tables serve the Revenue-at-Risk dashboard and support all three
# recovery scenarios: payment failure, checkout abandonment, overdue receivables.
# ---------------------------------------------------------------------------

class RecoveryScenario(str, enum.Enum):
    """Which structural problem type this recovery case represents.

    PAYMENT_FAILURE   - Customer attempted payment, transaction declined/timed-out.
                        Intent to pay exists; the problem is transactional.
    CHECKOUT_ABANDONMENT - Order/link created, customer never attempted payment.
                        No failure event; the signal is silence past a window.
    OVERDUE_RECEIVABLE - Invoice or subscription unpaid past its due date.
                        Slower, relationship-sensitive; closer to collections.
    """
    PAYMENT_FAILURE = "payment_failure"
    CHECKOUT_ABANDONMENT = "checkout_abandonment"
    OVERDUE_RECEIVABLE = "overdue_receivable"


class CaseStatus(str, enum.Enum):
    """Lifecycle state of a RecoveryCase.

    NEW         - Just created, no action taken yet.
    AT_RISK     - Identified as at-risk but intervention not yet started.
    INTERVENING - At least one action in the ladder has been attempted.
    RECOVERED   - Confirmed payment received (amount_recovered > 0).
    LOST        - Decisively unrecoverable (max ladder steps exhausted, customer disputed, etc.)
    ESCALATED   - Handed off to a human for manual handling.
    """
    NEW = "new"
    AT_RISK = "at_risk"
    INTERVENING = "intervening"
    RECOVERED = "recovered"
    LOST = "lost"
    ESCALATED = "escalated"


class ClassificationMethod(str, enum.Enum):
    """How the scenario / error source was determined."""
    RULE = "rule"
    LLM = "llm"


class InvoiceStatus(str, enum.Enum):
    """Lifecycle state of a merchant Invoice (overdue receivables surface).

    PENDING          - Issued, not yet due.
    PAID             - Payment confirmed.
    OVERDUE          - Past due_date, unpaid.
    CANCELLED        - Voided by merchant.
    WRITE_OFF_REVIEW - Escalated for write-off consideration (max ladder exhausted).
    """
    PENDING = "pending"
    PAID = "paid"
    OVERDUE = "overdue"
    CANCELLED = "cancelled"
    WRITE_OFF_REVIEW = "write_off_review"


class Invoice(Base):
    """A merchant-issued invoice, the source anchor for OVERDUE_RECEIVABLE cases.

    Design decisions:
    - merchant_id is non-nullable: every invoice belongs to exactly one merchant.
    - customer_user_id is nullable: the customer may not have a portal account
      (e.g. B2B invoices sent to an email-only contact).
    - invoice_number is unique across the whole platform (merchant-scoped
      uniqueness would require a composite unique; keeping it simple for now).
    - razorpay_invoice_id stores the Razorpay Invoices API id (e.g. "inv_...")
      once the invoice has been pushed to Razorpay.
    - payment_link_url is populated once a Payment Link is created for dunning
      (Day-0 dunning action or subsequent ladder steps).
    """

    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    merchant_id = Column(Integer, ForeignKey("merchant_users.id"), nullable=False, index=True)
    customer_user_id = Column(Integer, ForeignKey("customer_users.id"), nullable=True, index=True)

    invoice_number = Column(String(100), unique=True, nullable=False)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), default="INR", nullable=False)

    issue_date = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    due_date = Column(DateTime, nullable=False)

    status = Column(SAEnum(InvoiceStatus), default=InvoiceStatus.PENDING, nullable=False, index=True)

    # Razorpay integration fields (populated after push to Razorpay APIs)
    razorpay_invoice_id = Column(String(100), nullable=True)   # e.g. "inv_..."
    payment_link_url = Column(String(500), nullable=True)

    paid_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    merchant = relationship("MerchantUser", foreign_keys=[merchant_id])
    customer = relationship("CustomerUser", foreign_keys=[customer_user_id])


class RecoveryCase(Base):
    """Unified audit row that covers all three recovery scenarios.

    Design decisions:
    - checkout_session_id links to an existing CheckoutSession for the two
      checkout-related scenarios. Nullable because OVERDUE_RECEIVABLE cases
      may originate from an invoice, not a storefront checkout.
    - invoice_id is a proper FK to invoices.id (nullable). Payment-failure
      and checkout-abandonment cases always have NULL here; only
      OVERDUE_RECEIVABLE cases populate it.
    - amount_recovered starts at 0.0 and is updated ONLY when a CONFIRMED
      payment is received -- never on "offer sent" or "link created".
    - ladder_step tracks which step of the category-specific action ladder
      has been completed (0 = none attempted).
    - rar_score (Revenue-at-Risk score) is a 0-100 priority score used to
      sort the Revenue-at-Risk dashboard.
    - contact_touches counts how many outbound touches have been made so
      guardrails can enforce per-case retry limits.
    """

    __tablename__ = "recovery_cases"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Ownership
    merchant_id = Column(Integer, ForeignKey("merchant_users.id"), nullable=False, index=True)
    customer_user_id = Column(Integer, ForeignKey("customer_users.id"), nullable=True, index=True)

    # Source references (at least one should be set)
    checkout_session_id = Column(Integer, ForeignKey("checkout_sessions.id"), nullable=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=True, index=True)

    # Scenario classification
    scenario = Column(SAEnum(RecoveryScenario), nullable=False, index=True)

    # Financials  -- see docstring: recovered only updated on CONFIRMED payment
    amount_at_risk = Column(Float, nullable=False)
    amount_recovered = Column(Float, nullable=False, default=0.0)
    coupon_code_used = Column(String(50), nullable=True)
    discount_amount = Column(Float, nullable=False, default=0.0)

    # Case lifecycle
    status = Column(SAEnum(CaseStatus), nullable=False, default=CaseStatus.NEW, index=True)
    ladder_step = Column(Integer, nullable=False, default=0)

    # Diagnosis
    classification = Column(String(100), nullable=True)   # e.g. "card_declined", "accidental_close"
    classification_source = Column(SAEnum(ClassificationMethod), nullable=True)
    error_source = Column(String(200), nullable=True)     # human-readable source e.g. "gateway_timeout"
    rar_score = Column(Float, nullable=True)              # 0-100 priority score
    confidence = Column(Float, nullable=True)

    # Escalation
    escalated_to_human = Column(Boolean, nullable=False, default=False)
    escalation_reason = Column(Text, nullable=True)

    # Outreach tracking
    contact_touches = Column(Integer, nullable=False, default=0)
    last_action_at = Column(DateTime, nullable=True)
    next_action_due_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    merchant = relationship("MerchantUser", foreign_keys=[merchant_id])
    customer = relationship("CustomerUser", foreign_keys=[customer_user_id])
    checkout_session = relationship("CheckoutSession", foreign_keys=[checkout_session_id])
    invoice = relationship("Invoice", foreign_keys=[invoice_id])
    action_logs = relationship("RecoveryActionLog", back_populates="case",
                               order_by="RecoveryActionLog.created_at")


class RecoveryActionLog(Base):
    """Append-only audit log for every action attempted on a RecoveryCase.

    One row per attempted step in the ladder.  idempotency_key ensures that
    if the same ladder step / action type fires twice (e.g. retry on timeout),
    the second insert raises an IntegrityError instead of creating a duplicate
    row, so the caller can catch and skip rather than double-sending.

    Design decisions:
    - guardrail_checks stores a JSON blob of which guardrails were evaluated
      and their result (passed/failed/skipped).  This gives the merchant an
      auditable trail of WHY an action was or wasn't taken.
    - requires_human_approval / approved_by / approved_at implement the
      finance-governance gate for OVERDUE_RECEIVABLE discount offers:
      the record is created but no action fires until approved_by is set.
    - amount_offered is the discounted amount sent to the customer.
      It is NEVER conflated with amount_recovered on RecoveryCase.
    """

    __tablename__ = "recovery_action_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(Integer, ForeignKey("recovery_cases.id"), nullable=False, index=True)

    # "{case_id}:{ladder_step}:{action_type}" -- enforces one row per step attempt
    idempotency_key = Column(String(200), unique=True, nullable=False)

    ladder_step = Column(Integer, nullable=False)
    action_type = Column(String(100), nullable=False)  # e.g. "send_email", "send_sms", "create_payment_link"
    reason = Column(Text, nullable=True)               # human-readable explanation of why this action was chosen
    guardrail_checks = Column(Text, nullable=True)     # JSON: {"opted_out": false, "max_retries": false, ...}

    outcome = Column(String(50), nullable=True)        # "sent", "failed", "skipped", "pending_approval"
    amount_offered = Column(Float, nullable=True)      # offer price, NOT recovered amount
    coupon_code = Column(String(50), nullable=True)

    # Finance-governance gate for OVERDUE_RECEIVABLE discounts
    requires_human_approval = Column(Boolean, nullable=False, default=False)
    approved_by = Column(Integer, ForeignKey("merchant_users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    # Relationships
    case = relationship("RecoveryCase", back_populates="action_logs")
    approver = relationship("MerchantUser", foreign_keys=[approved_by])