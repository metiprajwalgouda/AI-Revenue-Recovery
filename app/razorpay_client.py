"""
Thin wrapper around the Razorpay SDK.

Why a wrapper instead of calling razorpay.Client() everywhere:
1. Centralizes error handling (timeouts, rate limits, auth failures)
2. Makes the rest of the codebase TESTABLE without hitting the real API
   (we can swap this class for a fake one in tests)
3. One place to add retry/backoff logic later
"""

import os
import time
import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

import razorpay
from razorpay.errors import BadRequestError, ServerError

logger = logging.getLogger("recovery_agent.razorpay")


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class PaymentLinkResult:
    success: bool
    payment_link_id: Optional[str] = None
    short_url: Optional[str] = None
    error_message: Optional[str] = None
    error_type: Optional[str] = None  # "auth", "bad_request", "server_error", "timeout", "unknown"


class RazorpayRecoveryClient:
    """Wraps Razorpay test-mode API calls needed for checkout recovery."""

    def __init__(self, key_id: Optional[str] = None, key_secret: Optional[str] = None):
        self.key_id = key_id or os.getenv("RAZORPAY_KEY_ID")
        self.key_secret = key_secret or os.getenv("RAZORPAY_KEY_SECRET")

        if not self.key_id or not self.key_secret:
            raise ValueError(
                "Razorpay keys missing. Set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in your .env file."
            )

        self.client = razorpay.Client(auth=(self.key_id, self.key_secret))

    def create_recovery_payment_link(
        self,
        amount_rupees: float,
        customer_name: str,
        customer_email: str,
        customer_phone: str,
        description: str,
        reference_id: str,
        max_retries: int = 3,
    ) -> PaymentLinkResult:
        """Creates a Razorpay Payment Link for a customer to complete an abandoned checkout.
        Amount must be passed to Razorpay in paise (smallest currency unit), not rupees.

        Rate limiting: Razorpay test mode enforces a request rate limit. "Too many
        requests" is a TRANSIENT condition, not a bad record, so we retry with
        exponential backoff instead of counting it as a permanent failure.

        Duplicate reference_id is a SEPARATE, non-transient condition (Razorpay
        remembers reference_ids across every run forever) -- retrying never helps,
        so it gets its own distinct error_type instead of being lumped in with
        generic bad_request or treated as a rate limit.
        """
        if amount_rupees <= 0:
            return PaymentLinkResult(
                success=False,
                error_message=f"Invalid amount: {amount_rupees}. Must be > 0.",
                error_type="bad_request",
            )

        amount_paise = int(round(amount_rupees * 100))

        payload = {
            "amount": amount_paise,
            "currency": "INR",
            "accept_partial": False,
            "description": description,
            "customer": {
                "name": customer_name,
                "email": customer_email,
                "contact": customer_phone,
            },
            "notify": {"sms": True, "email": True},
            "reminder_enable": True,
            "reference_id": reference_id,
        }

        last_error = None
        for attempt in range(max_retries + 1):
            try:
                response = self.client.payment_link.create(payload)
                return PaymentLinkResult(
                    success=True,
                    payment_link_id=response.get("id"),
                    short_url=response.get("short_url"),
                )

            except BadRequestError as e:
                error_text = str(e)

                if "too many requests" in error_text.lower():
                    last_error = e
                    if attempt < max_retries:
                        backoff_sec = 2 ** (attempt + 1)  # 2s, 4s, 8s
                        logger.warning(
                            f"Rate limited on {reference_id}, retrying in {backoff_sec}s "
                            f"(attempt {attempt + 1}/{max_retries})"
                        )
                        time.sleep(backoff_sec)
                        continue
                    logger.error(f"Rate limited on {reference_id} after {max_retries} retries, giving up.")
                    return PaymentLinkResult(success=False, error_message=error_text, error_type="rate_limited")

                if "already exists" in error_text.lower():
                    logger.warning(f"Duplicate reference_id for {reference_id}: {e}")
                    return PaymentLinkResult(success=False, error_message=error_text, error_type="duplicate_reference_id")

                logger.warning(f"Razorpay bad request for {reference_id}: {e}")
                return PaymentLinkResult(success=False, error_message=error_text, error_type="bad_request")

            except ServerError as e:
                logger.error(f"Razorpay server error for {reference_id}: {e}")
                return PaymentLinkResult(success=False, error_message=str(e), error_type="server_error")

            except Exception as e:
                logger.error(f"Unexpected error creating payment link for {reference_id}: {e}")
                return PaymentLinkResult(success=False, error_message=str(e), error_type="unknown")

        return PaymentLinkResult(success=False, error_message=str(last_error), error_type="rate_limited")

    def fetch_payment_link_status(self, payment_link_id: str) -> Optional[str]:
        """Checks whether a previously sent payment link was paid, cancelled, or is still pending."""
        try:
            response = self.client.payment_link.fetch(payment_link_id)
            return response.get("status")  # "created", "paid", "cancelled", "expired"
        except Exception as e:
            logger.error(f"Could not fetch status for {payment_link_id}: {e}")
            return None

    def create_order(self, amount_rupees: float, receipt: str) -> OrderResult:
        """Creates a Razorpay Order -- required by Checkout.js (the real payment popup)
        for the FIRST-TIME storefront checkout. This is DIFFERENT from payment links:
        payment links are for the recovery agent to send a shareable pay-later URL;
        orders are for an immediate in-browser checkout popup on the storefront itself."""
        if amount_rupees <= 0:
            return OrderResult(success=False, error_message=f"Invalid amount: {amount_rupees}")

        amount_paise = int(round(amount_rupees * 100))
        try:
            response = self.client.order.create({
                "amount": amount_paise,
                "currency": "INR",
                "receipt": receipt,
                "payment_capture": 1,  # auto-capture on successful payment
            })
            return OrderResult(success=True, order_id=response.get("id"))
        except Exception as e:
            logger.error(f"Order creation failed for receipt {receipt}: {e}")
            return OrderResult(success=False, error_message=str(e))

    def verify_payment_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        """Verifies that a payment success callback genuinely came from Razorpay and
        wasn't forged client-side. CRITICAL: never trust a frontend 'payment succeeded'
        callback without this check -- anyone could otherwise call your /complete
        endpoint directly with fake IDs and mark an unpaid order as paid."""
        try:
            self.client.utility.verify_payment_signature({
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
                "razorpay_signature": signature,
            })
            return True
        except razorpay.errors.SignatureVerificationError:
            logger.error(f"Signature verification FAILED for order {order_id} -- possible forged request.")
            return False
        except Exception as e:
            logger.error(f"Signature verification error for order {order_id}: {e}")
            return False


class SimulatedRazorpayClient:
    """Drop-in replacement for RazorpayRecoveryClient, implementing the SAME interface,
    used for full-batch runs.

    WHY THIS EXISTS (found during real testing, see CHALLENGES.md):
    Razorpay's test mode enforces a hard cap of 30 payment links total on the account --
    not a rate limit you can retry past, an absolute ceiling. Running an 86-event batch
    against the live API every time we test is therefore impossible past the first ~30,
    and wastes real quota on repeated test runs during development.

    This simulator produces DETERMINISTIC, reproducible outcomes (seeded by reference_id,
    not global random state, so results don't depend on call order) so that full-batch
    dashboard metrics are stable and honestly labeled as simulated. The LIVE client is
    still used and separately verified against a small real subset (see run_pipeline.py
    --mode flag) to prove the actual Razorpay integration works end to end.
    """

    def __init__(self, paid_rate: float = 0.35, failure_rate: float = 0.05):
        self.paid_rate = paid_rate
        self.failure_rate = failure_rate
        self._link_statuses: dict[str, str] = {}  # in-memory "database" of simulated links

    def _seeded_random(self, reference_id: str) -> float:
        """Deterministic pseudo-random value in [0, 1) derived from reference_id,
        so the same event always gets the same simulated outcome regardless of run order."""
        h = hashlib.sha256(reference_id.encode()).hexdigest()
        return int(h[:8], 16) / 0xFFFFFFFF

    def create_recovery_payment_link(
        self,
        amount_rupees: float,
        customer_name: str,
        customer_email: str,
        customer_phone: str,
        description: str,
        reference_id: str,
        max_retries: int = 3,
    ) -> PaymentLinkResult:
        if amount_rupees <= 0:
            return PaymentLinkResult(
                success=False,
                error_message=f"Invalid amount: {amount_rupees}. Must be > 0.",
                error_type="bad_request",
            )

        creation_roll = self._seeded_random(reference_id)
        if creation_roll < self.failure_rate:
            return PaymentLinkResult(
                success=False,
                error_message="SIMULATED: link creation failed (simulated bad request).",
                error_type="bad_request",
            )

        link_id = f"plink_sim_{hashlib.sha256(reference_id.encode()).hexdigest()[:14]}"

        # IMPORTANT: status is seeded by link_id, NOT reference_id. This is what
        # reconcile_pipeline.py has access to later (RecoveryOutcome stores
        # payment_link_id, not the original reference_id) -- seeding status by
        # link_id means a fresh SimulatedRazorpayClient instance in a separate
        # script run can deterministically re-derive the SAME status, so creation-time
        # and reconciliation-time results always agree. (Seeding by reference_id here
        # would silently produce DIFFERENT results at reconcile time -- a real bug
        # caught while wiring up the two-script flow, see CHALLENGES.md.)
        status_roll = self._seeded_random(link_id)
        self._link_statuses[link_id] = "paid" if status_roll < self.paid_rate else "created"

        return PaymentLinkResult(
            success=True,
            payment_link_id=link_id,
            short_url=f"https://rzp.io/i/simulated_{link_id[-8:]}",
        )

    def fetch_payment_link_status(self, payment_link_id: str) -> Optional[str]:
        if payment_link_id in self._link_statuses:
            return self._link_statuses[payment_link_id]
        # Fresh instance (e.g. a separate reconcile script run) with no in-memory
        # history -- re-derive deterministically from the link_id itself.
        status_roll = self._seeded_random(payment_link_id)
        return "paid" if status_roll < self.paid_rate else "created"