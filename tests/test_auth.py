"""
Tests for password hashing and session token utilities.
"""

import os
os.environ.setdefault("SESSION_SECRET_KEY", "test-secret-key-for-testing-only")

import time
import pytest
from app.auth import (
    hash_password, verify_password,
    create_merchant_session_token, decode_merchant_session_token,
    create_customer_session_token, decode_customer_session_token,
)


# ---------- Password hashing ----------

def test_password_hash_and_verify_roundtrip():
    hashed = hash_password("correct-horse-battery-staple")
    assert verify_password("correct-horse-battery-staple", hashed) is True


def test_wrong_password_rejected():
    hashed = hash_password("real-password")
    assert verify_password("wrong-password", hashed) is False


def test_password_hash_is_never_the_plaintext():
    hashed = hash_password("my-secret-password")
    assert hashed != "my-secret-password"
    assert "my-secret-password" not in hashed


def test_same_password_produces_different_hashes():
    """bcrypt salts automatically -- two hashes of the same password must differ,
    otherwise identical passwords would be visibly identical in the database."""
    hash1 = hash_password("same-password")
    hash2 = hash_password("same-password")
    assert hash1 != hash2
    assert verify_password("same-password", hash1)
    assert verify_password("same-password", hash2)


def test_verify_password_handles_malformed_hash_safely():
    """Edge case: a corrupted/malformed hash in the DB must fail closed
    (reject login) rather than raise a 500 error."""
    assert verify_password("anything", "not-a-real-bcrypt-hash") is False


# ---------- Session tokens ----------

def test_merchant_token_roundtrip():
    token = create_merchant_session_token(merchant_id=42)
    assert decode_merchant_session_token(token) == 42


def test_customer_token_roundtrip():
    token = create_customer_session_token(customer_id=7)
    assert decode_customer_session_token(token) == 7


def test_tampered_token_rejected():
    token = create_merchant_session_token(merchant_id=1)
    tampered = token[:-4] + "abcd"  # corrupt the signature
    assert decode_merchant_session_token(tampered) is None


def test_garbage_token_rejected():
    assert decode_merchant_session_token("not-a-real-token-at-all") is None
    assert decode_customer_session_token("also-garbage") is None


def test_merchant_token_rejected_by_customer_decoder():
    """CRITICAL security test: a valid MERCHANT token must be rejected when
    decoded as a CUSTOMER token, and vice versa -- this is the entire point
    of using separate signing salts instead of just separate cookie names."""
    merchant_token = create_merchant_session_token(merchant_id=99)
    assert decode_customer_session_token(merchant_token) is None


def test_customer_token_rejected_by_merchant_decoder():
    customer_token = create_customer_session_token(customer_id=99)
    assert decode_merchant_session_token(customer_token) is None


def test_expired_token_rejected(monkeypatch):
    """Simulates an expired session by patching max_age to 0 and waiting a moment,
    rather than waiting 7 real days in a test."""
    import app.auth as auth_module
    monkeypatch.setattr(auth_module, "SESSION_MAX_AGE_SECONDS", 0)
    token = create_merchant_session_token(merchant_id=5)
    time.sleep(1.1)
    assert decode_merchant_session_token(token) is None