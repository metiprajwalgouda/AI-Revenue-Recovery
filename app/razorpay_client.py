"""
Thin wrapper around the Razorpay SDK.

Why a wrapper instead of calling razorpay.Client() everywhere:
1. Centralizes error handling (timeouts, rate limits, auth failures)
2. Makes the rest of the codebase TESTABLE without hitting the real API
   (we can swap this class for a fake one in tests)
3. One place to add retry/backoff logic later
"""

import os
import logging
from dataclasses import dataclass
from typing import Optional

import razorpay
from razorpay.errors import BadRequestError, ServerError

logger = logging.getLogger("recovery_agent.razorpay")


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
    ) -> PaymentLinkResult:
        """Creates a Razorpay Payment Link for a customer to complete an abandoned checkout.
        Amount must be passed to Razorpay in paise (smallest currency unit), not rupees.
        """
        if amount_rupees <= 0:
            # Guardrail: never attempt to create a payment link for zero/negative amounts.
            # This is one of our deliberate edge cases from the dataset.
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

        try:
            response = self.client.payment_link.create(payload)
            return PaymentLinkResult(
                success=True,
                payment_link_id=response.get("id"),
                short_url=response.get("short_url"),
            )

        except BadRequestError as e:
            # e.g. malformed payload, invalid phone/email format
            logger.warning(f"Razorpay bad request for {reference_id}: {e}")
            return PaymentLinkResult(success=False, error_message=str(e), error_type="bad_request")

        except ServerError as e:
            # Razorpay-side issue -- worth retrying later, not the customer's fault
            logger.error(f"Razorpay server error for {reference_id}: {e}")
            return PaymentLinkResult(success=False, error_message=str(e), error_type="server_error")

        except Exception as e:
            # Catch-all so ONE bad record never crashes the whole batch run.
            # This is exactly the kind of thing to mention in CHALLENGES.md.
            logger.error(f"Unexpected error creating payment link for {reference_id}: {e}")
            return PaymentLinkResult(success=False, error_message=str(e), error_type="unknown")

    def fetch_payment_link_status(self, payment_link_id: str) -> Optional[str]:
        """Checks whether a previously sent payment link was paid, cancelled, or is still pending."""
        try:
            response = self.client.payment_link.fetch(payment_link_id)
            return response.get("status")  # "created", "paid", "cancelled", "expired"
        except Exception as e:
            logger.error(f"Could not fetch status for {payment_link_id}: {e}")
            return None