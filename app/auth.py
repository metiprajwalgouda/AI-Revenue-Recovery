"""
Authentication utilities: password hashing + signed session tokens.

CRITICAL DESIGN DECISION: merchant and customer sessions use DIFFERENT signing
salts ("merchant-session" vs "customer-session"), not just different cookie
NAMES. This means a token issued for a customer is cryptographically invalid
if presented as a merchant token, even if someone manually copies a cookie
value from one cookie slot to the other. Using the same secret+salt for both
and relying only on the cookie name to keep them apart would mean a single
leaked/guessed secret compromises both account systems at once, and a bug
that reads the wrong cookie name could silently authenticate the wrong role.
Separate salts make cross-role token reuse fail at the cryptographic layer,
not just the application logic layer.
"""

import os
import bcrypt
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

SECRET_KEY = os.getenv("SESSION_SECRET_KEY", "dev-only-insecure-secret-change-in-env")
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 7  # 7 days

_merchant_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="merchant-session")
_customer_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="customer-session")

MERCHANT_COOKIE_NAME = "merchant_session"
CUSTOMER_COOKIE_NAME = "customer_session"


def hash_password(plain_password: str) -> str:
    hashed = bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed hash in the DB (shouldn't happen, but never let a bad
        # hash format crash login with a 500 -- treat it as "wrong password").
        return False


def create_merchant_session_token(merchant_id: int) -> str:
    return _merchant_serializer.dumps({"merchant_id": merchant_id})


def create_customer_session_token(customer_id: int) -> str:
    return _customer_serializer.dumps({"customer_id": customer_id})


def decode_merchant_session_token(token: str) -> int | None:
    """Returns the merchant_id if the token is valid and unexpired, else None.
    Never raises -- callers treat None as 'not logged in', not as an error."""
    try:
        data = _merchant_serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
        return data.get("merchant_id")
    except (BadSignature, SignatureExpired):
        return None


def decode_customer_session_token(token: str) -> int | None:
    try:
        data = _customer_serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
        return data.get("customer_id")
    except (BadSignature, SignatureExpired):
        return None