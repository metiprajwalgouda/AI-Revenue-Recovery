"""
Receivables Dunning Ladder — reconcile_overdue_invoices()
=========================================================

Standalone, idempotent poller that advances Invoice/RecoveryCase rows through
the four-step ladder. Designed to be called repeatedly (every N minutes) safely.

Ladder definition (v1, no subscriptions/mandates):

  Step 1 — Day 0 (due_date reached):
    Invoice.status -> OVERDUE
    RecoveryCase.status -> INTERVENING
    Send reminder email (template-only, exact invoice amount, NO discount).
    Set next_action_due_at = due_date + 7 days.

  Step 2 — +7 days:
    Send 2nd reminder email. Same template.
    Set next_action_due_at = due_date + 30 days.
    Set RecoveryCase.ladder_step = 2.

  Step 3 — +30 days:
    RecoveryCase.status = ESCALATED, escalated_to_human = True.
    escalation_reason = "receivable_30d_overdue".
    NO outbound contact from this point — hard ceiling.
    Set next_action_due_at = due_date + 60 days.
    Set RecoveryCase.ladder_step = 3.

  Step 4 — +60 days:
    Invoice.status -> WRITE_OFF_REVIEW.
    Finance-review flag only — no contact, no case-status change.
    Clear next_action_due_at (ladder ends).
    Set RecoveryCase.ladder_step = 4.

Guardrail invariants (never broken):
  - Discounts / coupons are NEVER offered autonomously in any step.
  - No contact after ESCALATED (step >= 3 contact block).
  - Idempotency key "{case_id}:{ladder_step}:{action_type}" prevents double-fire.
  - Already-RECOVERED or already-LOST cases are skipped silently.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.db_models import (
    Invoice, InvoiceStatus,
    RecoveryCase, RecoveryActionLog,
    RecoveryScenario, CaseStatus,
    MerchantUser, CustomerUser,
)
from app.email_service import send_recovery_email
from app.razorpay_client import get_razorpay_client

logger = logging.getLogger("recovery_agent.receivables_dunning")


# ---------------------------------------------------------------------------
# Ladder thresholds
# ---------------------------------------------------------------------------
LADDER = {
    # step: (label, days_after_due, contact_allowed)
    1: ("day_0_reminder",    0,  True),
    2: ("day_7_reminder",    7,  True),
    3: ("day_30_escalation", 30, False),  # contact STOPS here
    4: ("day_60_write_off",  60, False),
}

# No contact is ever sent at step >= 3
CONTACT_CUTOFF_STEP = 3


def _case_is_terminal(case: RecoveryCase) -> bool:
    """True if the case is already RECOVERED or LOST — skip silently."""
    return case.status in (CaseStatus.RECOVERED, CaseStatus.LOST)


def _build_idempotency_key(case_id: int, ladder_step: int, action_type: str) -> str:
    return f"{case_id}:{ladder_step}:{action_type}"


def _send_invoice_reminder(
    case: RecoveryCase,
    invoice: Invoice,
    customer: CustomerUser,
    merchant: MerchantUser,
    ladder_step: int,
) -> str:
    """Sends the reminder email. Returns delivery outcome string."""
    if not customer or not customer.email:
        logger.warning("No customer email for invoice %s — skipping send", invoice.invoice_number)
        return "skipped_no_email"

    due_date_str = invoice.due_date.strftime("%d %b %Y") if invoice.due_date else "N/A"
    is_overdue = invoice.due_date and invoice.due_date.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)

    context = {
        "store_name": merchant.store_name if merchant else "Our Store",
        "customer_name": customer.name or customer.email,
        "invoice_number": invoice.invoice_number,
        "amount": invoice.amount,
        "currency": invoice.currency or "INR",
        "due_date_str": due_date_str,
        "is_overdue": is_overdue,
        "payment_link_url": invoice.payment_link_url,  # may be None
        "ladder_step": ladder_step,
        # Explicitly NO discount fields — template must not render any discount
    }

    subject = (
        f"[Reminder {ladder_step}/2] Invoice {invoice.invoice_number} — "
        f"{invoice.currency or 'INR'} {invoice.amount:.2f} "
        f"{'OVERDUE' if is_overdue else 'Due'}"
    )

    sent = send_recovery_email(
        to=customer.email,
        subject=subject,
        template_name="emails/invoice_reminder.html",
        context=context,
    )
    return "sent" if sent else "failed"


def _write_action_log(
    db: Session,
    case: RecoveryCase,
    ladder_step: int,
    action_type: str,
    outcome: str,
    reason: str,
    guardrail_dict: dict,
    contact_made: bool,
) -> Optional[RecoveryActionLog]:
    """
    Write a RecoveryActionLog row. Returns the log on success, None if the
    idempotency key already exists (safe duplicate — already processed).
    """
    ikey = _build_idempotency_key(case.id, ladder_step, action_type)
    existing = db.query(RecoveryActionLog).filter_by(idempotency_key=ikey).first()
    if existing:
        return None

    log = RecoveryActionLog(
        case_id=case.id,
        idempotency_key=ikey,
        ladder_step=ladder_step,
        action_type=action_type,
        reason=reason,
        guardrail_checks=json.dumps({
            **guardrail_dict,
            "ladder_step": ladder_step,
            "contact_made": contact_made,
            "discount_offered": False,  # invariant — always False for dunning
        }),
        outcome=outcome,
        amount_offered=None,    # never offer a discounted amount
        coupon_code=None,       # never attach a coupon
        requires_human_approval=False,
    )
    try:
        with db.begin_nested():
            db.add(log)
            db.flush()
        return log
    except (IntegrityError, Exception) as exc:
        logger.info(
            "Idempotency / flush conflict for case %s step %s action %s (%s) — skipping",
            case.id, ladder_step, action_type, exc,
        )
        return None



def _advance_step1(db: Session, invoice: Invoice, case: RecoveryCase, now: datetime) -> bool:
    """Day 0: due_date reached. Flip invoice OVERDUE, send first reminder.

    Ordering (matches payment_failure_actions.py):
      1. Prepare context & guardrail checks.
      2. Write the audit log row with outcome="pending" and flush — this is the
         idempotency claim. If a concurrent caller (background thread + /internal/reconcile
         race) already wrote the same idempotency key, _write_action_log returns None.
      3. If None → abort (concurrent caller owns this step). No email sent.
      4. Flip status and commit claim before any network call.
      5. Send the reminder email (fire-and-forget; failures don't roll back).
      6. Patch log.outcome to "sent"/"failed" and commit.
    """
    customer: Optional[CustomerUser] = invoice.customer
    merchant: Optional[MerchantUser] = db.query(MerchantUser).filter(
        MerchantUser.id == case.merchant_id
    ).first()

    contact_possible = bool(customer and not customer.opted_out_of_marketing and customer.email)
    guardrail_dict = {
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "amount": invoice.amount,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
    }

    # 1. CLAIM the log row — outcome="pending" is the concurrency fence
    log = _write_action_log(
        db, case, 1, "send_invoice_reminder",
        outcome="pending",  # will be patched after send
        reason="Invoice past due — Day 0 first reminder sent.",
        guardrail_dict=guardrail_dict,
        contact_made=False,  # will be updated after send
    )

    # 2. CHECK — if None, concurrent caller already claimed this step. Abort.
    if log is None:
        logger.info(
            "Step 1 idempotency hit for case %s (invoice %s) — aborting, no email sent.",
            case.id, invoice.invoice_number,
        )
        return False

    # Flip invoice and advance case fields
    invoice.status = InvoiceStatus.OVERDUE
    case.status = CaseStatus.INTERVENING
    case.ladder_step = 1
    case.last_action_at = now
    case.next_action_due_at = invoice.due_date.replace(tzinfo=timezone.utc) + timedelta(days=7)

    # 3. COMMIT the claim before any network call
    db.commit()

    # 4. SEND (fire-and-forget)
    if contact_possible:
        delivery_status = _send_invoice_reminder(case, invoice, customer, merchant, ladder_step=1)
    else:
        delivery_status = "skipped_opted_out" if customer else "skipped_no_customer"

    # 5. PATCH outcome now that we know whether the send succeeded
    log.outcome = delivery_status
    log.guardrail_checks = json.dumps({
        **guardrail_dict,
        "ladder_step": 1,
        "contact_made": (delivery_status == "sent"),
        "discount_offered": False,
    })
    db.commit()
    return True


def _advance_step2(db: Session, invoice: Invoice, case: RecoveryCase, now: datetime) -> bool:
    """+7 days: send second reminder. No new channel.

    Same write-then-check-then-send ordering as _advance_step1.
    """
    customer: Optional[CustomerUser] = invoice.customer
    merchant: Optional[MerchantUser] = db.query(MerchantUser).filter(
        MerchantUser.id == case.merchant_id
    ).first()

    contact_possible = bool(customer and not customer.opted_out_of_marketing and customer.email)
    guardrail_dict = {
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "amount": invoice.amount,
        "days_overdue": 7,
    }

    # 1. CLAIM
    log = _write_action_log(
        db, case, 2, "send_invoice_reminder",
        outcome="pending",
        reason="Invoice 7 days overdue — Day 7 second reminder sent.",
        guardrail_dict=guardrail_dict,
        contact_made=False,
    )

    # 2. CHECK
    if log is None:
        logger.info(
            "Step 2 idempotency hit for case %s (invoice %s) — aborting, no email sent.",
            case.id, invoice.invoice_number,
        )
        return False

    # Advance case fields
    case.ladder_step = 2
    case.last_action_at = now
    case.next_action_due_at = invoice.due_date.replace(tzinfo=timezone.utc) + timedelta(days=30)

    # 3. COMMIT claim
    db.commit()

    # 4. SEND
    if contact_possible:
        delivery_status = _send_invoice_reminder(case, invoice, customer, merchant, ladder_step=2)
    else:
        delivery_status = "skipped_opted_out" if customer else "skipped_no_customer"

    # 5. PATCH outcome
    log.outcome = delivery_status
    log.guardrail_checks = json.dumps({
        **guardrail_dict,
        "ladder_step": 2,
        "contact_made": (delivery_status == "sent"),
        "discount_offered": False,
    })
    db.commit()
    return True


def _advance_step3(db: Session, invoice: Invoice, case: RecoveryCase, now: datetime) -> bool:
    """+30 days: mandatory human escalation. Contact stops completely."""
    guardrail_dict = {
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "amount": invoice.amount,
        "days_overdue": 30,
        "escalation_reason": "receivable_30d_overdue",
        "contact_blocked": True,  # hard ceiling — no further autonomous contact
    }

    # No email/SMS/call — this is purely a flag for human review
    log = _write_action_log(
        db, case, 3, "escalate_to_human",
        outcome="escalated",
        reason="Invoice 30 days overdue — mandatory escalation. Autonomous contact stops.",
        guardrail_dict=guardrail_dict,
        contact_made=False,  # NEVER contact after escalation
    )
    if log is None:
        return False

    case.status = CaseStatus.ESCALATED
    case.escalated_to_human = True
    case.escalation_reason = "receivable_30d_overdue"
    case.ladder_step = 3
    case.last_action_at = now
    case.next_action_due_at = invoice.due_date.replace(tzinfo=timezone.utc) + timedelta(days=60)
    db.commit()
    return True


def _advance_step4(db: Session, invoice: Invoice, case: RecoveryCase, now: datetime) -> bool:
    """+60 days: flag invoice WRITE_OFF_REVIEW. Finance-only. No contact."""
    guardrail_dict = {
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "amount": invoice.amount,
        "days_overdue": 60,
        "contact_blocked": True,
    }

    # No case status change beyond ESCALATED (already set at step 3)
    # No contact of any kind
    log = _write_action_log(
        db, case, 4, "flag_write_off_review",
        outcome="flagged",
        reason="Invoice 60 days overdue — flagged for write-off review by finance. No autonomous action.",
        guardrail_dict=guardrail_dict,
        contact_made=False,
    )
    if log is None:
        return False

    invoice.status = InvoiceStatus.WRITE_OFF_REVIEW
    case.ladder_step = 4
    case.last_action_at = now
    case.next_action_due_at = None  # ladder ends
    db.commit()
    return True


def process_invoice_payment(
    db: Session,
    invoice: Invoice,
    razorpay_payment_id: str,
    amount_paid: float,
    channel: str = "webhook",
) -> Optional[RecoveryCase]:
    """Marks an invoice as PAID and its linked RecoveryCase as RECOVERED,
    writing an idempotent audit log row and cancelling future dunning steps.
    """
    case = db.query(RecoveryCase).filter(
        RecoveryCase.invoice_id == invoice.id,
        RecoveryCase.scenario == RecoveryScenario.OVERDUE_RECEIVABLE,
    ).first()

    if invoice.status == InvoiceStatus.PAID and (case is None or case.status == CaseStatus.RECOVERED):
        return case

    invoice.status = InvoiceStatus.PAID
    now = datetime.now(timezone.utc)

    if case is not None and case.status != CaseStatus.RECOVERED:
        case.status = CaseStatus.RECOVERED
        case.amount_recovered = amount_paid
        case.last_action_at = now
        case.next_action_due_at = None   # cancel all future ladder steps

        # Idempotent audit log — same pattern as complete_checkout()
        ikey = f"{case.id}:recovered:inv_{invoice.id}:{razorpay_payment_id}"
        log = RecoveryActionLog(
            case_id=case.id,
            idempotency_key=ikey,
            ladder_step=case.ladder_step,
            action_type="case_recovered",
            reason=f"Invoice {invoice.invoice_number} paid via Razorpay — case closed as RECOVERED ({channel}).",
            guardrail_checks=json.dumps({
                "confirmed_payment_id": razorpay_payment_id,
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "amount_paid": amount_paid,
                "discount_offered": False,  # invariant
                "channel": channel,
            }),
            outcome="sent",
            amount_offered=None,
            coupon_code=None,
            requires_human_approval=False,
        )
        db.add(log)
        try:
            db.flush()
        except Exception:
            db.rollback()
            logger.warning(
                "Duplicate invoice payment complete for invoice_id=%s — skipping audit log",
                invoice.id,
            )

    db.commit()
    if case:
        db.refresh(case)
    db.refresh(invoice)
    return case


def reconcile_overdue_invoices(db: Session) -> dict:
    """
    Idempotent poller. Safe to call as frequently as every minute.

    Phase 0: query Razorpay API for unpaid invoices with payment links. If paid,
             mark PAID/RECOVERED immediately (fallback for missed webhooks/redirects).
    Phase 1: find PENDING invoices whose due_date has passed → flip to OVERDUE
             and fire step 1.
    Phase 2: find cases whose next_action_due_at has passed → advance ladder.

    Returns a summary dict for logging / the /internal/reconcile response.
    """
    now = datetime.now(timezone.utc)
    summary = {
        "recovered_via_poller": 0,
        "poller_reconciled_paid": 0,
        "new_overdue": 0,
        "step1_sent": 0,
        "step2_sent": 0,
        "step3_escalated": 0,
        "step4_write_off": 0,
        "skipped_terminal": 0,
        "idempotency_skips": 0,
    }

    # ── Phase 0: Payment reconciliation via Razorpay API poller ────────────────
    # Fallback in case webhooks or redirect callbacks did not arrive
    try:
        rzp = get_razorpay_client()
        unpaid_invoices_with_link = (
            db.query(Invoice)
            .filter(
                Invoice.status.in_([InvoiceStatus.PENDING, InvoiceStatus.OVERDUE]),
                Invoice.razorpay_invoice_id.isnot(None),
            )
            .all()
        )
        for inv in unpaid_invoices_with_link:
            status = rzp.fetch_payment_link_status(inv.razorpay_invoice_id)
            if status == "paid":
                logger.info(
                    "Poller detected Razorpay payment link %s as PAID for invoice %s",
                    inv.razorpay_invoice_id, inv.invoice_number
                )
                process_invoice_payment(
                    db=db,
                    invoice=inv,
                    razorpay_payment_id=f"poll_{inv.razorpay_invoice_id}",
                    amount_paid=inv.amount,
                    channel="poller_reconciliation",
                )
                summary["recovered_via_poller"] += 1
                summary["poller_reconciled_paid"] += 1
    except Exception as exc:
        logger.warning("Error during poller payment link status fetch: %s", exc)

    # ── Phase 1: PENDING invoices past due_date ────────────────────────────────
    pending_due = (
        db.query(Invoice)
        .filter(
            Invoice.status == InvoiceStatus.PENDING,
            Invoice.due_date <= now,
        )
        .all()
    )

    for invoice in pending_due:
        # Find the linked RecoveryCase
        case = (
            db.query(RecoveryCase)
            .filter(
                RecoveryCase.invoice_id == invoice.id,
                RecoveryCase.scenario == RecoveryScenario.OVERDUE_RECEIVABLE,
            )
            .first()
        )
        if case is None:
            logger.warning(
                "PENDING invoice %s has no linked RecoveryCase — skipping",
                invoice.invoice_number,
            )
            continue

        if _case_is_terminal(case):
            summary["skipped_terminal"] += 1
            continue

        if case.ladder_step >= 1:
            # Already advanced — idempotency guard (shouldn't happen in Phase 1,
            # but protects against race conditions)
            summary["idempotency_skips"] += 1
            continue

        logger.info("Phase 1: advancing invoice %s to step 1", invoice.invoice_number)
        if _advance_step1(db, invoice, case, now):
            summary["new_overdue"] += 1
            summary["step1_sent"] += 1
        else:
            summary["idempotency_skips"] += 1

    # ── Phase 2: OVERDUE cases with next_action_due_at in the past ────────────
    cases_due = (
        db.query(RecoveryCase)
        .filter(
            RecoveryCase.scenario == RecoveryScenario.OVERDUE_RECEIVABLE,
            RecoveryCase.next_action_due_at <= now,
            RecoveryCase.next_action_due_at.isnot(None),
        )
        .all()
    )

    for case in cases_due:
        if _case_is_terminal(case):
            summary["skipped_terminal"] += 1
            continue

        invoice = case.invoice
        if invoice is None:
            logger.warning("RecoveryCase %s has no linked invoice — skipping", case.id)
            continue

        next_step = case.ladder_step + 1
        logger.info(
            "Phase 2: case %s invoice %s → step %s",
            case.id, invoice.invoice_number, next_step,
        )

        if next_step == 2:
            if _advance_step2(db, invoice, case, now):
                summary["step2_sent"] += 1
            else:
                summary["idempotency_skips"] += 1
        elif next_step == 3:
            if _advance_step3(db, invoice, case, now):
                summary["step3_escalated"] += 1
            else:
                summary["idempotency_skips"] += 1
        elif next_step == 4:
            if _advance_step4(db, invoice, case, now):
                summary["step4_write_off"] += 1
            else:
                summary["idempotency_skips"] += 1
        else:
            # Step 4 was the final step — clear next_action_due_at defensively
            if case.next_action_due_at is not None:
                case.next_action_due_at = None
            logger.info("Case %s: ladder complete at step %s", case.id, case.ladder_step)

    db.commit()
    logger.info("reconcile_overdue_invoices complete: %s", summary)
    return summary
