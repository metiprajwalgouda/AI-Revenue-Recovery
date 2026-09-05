"""
Merchant signup/login/logout endpoints, and the get_current_merchant dependency
used to protect product-management routes.

Kept in a separate module from main.py (rather than inline) so main.py doesn't
become a single giant file as the app grows -- this module owns everything
merchant-authentication-related.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr, Field, ConfigDict

from app.db import get_db
from app.db_models import MerchantUser
from app.auth import (
    hash_password, verify_password,
    create_merchant_session_token, decode_merchant_session_token,
    MERCHANT_COOKIE_NAME, SESSION_MAX_AGE_SECONDS,
)

router = APIRouter(prefix="/api/merchant", tags=["merchant-auth"])


class MerchantSignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=200)
    store_name: str = Field(..., min_length=1, max_length=200)


class MerchantLoginRequest(BaseModel):
    email: EmailStr
    password: str


class MerchantOut(BaseModel):
    id: int
    email: str
    store_name: str

    model_config = ConfigDict(from_attributes=True)


def _set_merchant_cookie(response: Response, merchant_id: int) -> None:
    token = create_merchant_session_token(merchant_id)
    response.set_cookie(
        key=MERCHANT_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,       # never accessible to JS -- mitigates XSS token theft
        samesite="lax",      # sent on normal navigation, blocked on most cross-site requests
        # secure=True should be added once served over HTTPS in production;
        # left off here so local http://127.0.0.1 development keeps working.
    )

@router.post("/logout")
def logout_merchant(response: Response):
    response.delete_cookie(MERCHANT_COOKIE_NAME)
    return {"message": "Logged out successfully"}


def get_current_merchant(request: Request, db: Session = Depends(get_db)) -> MerchantUser:
    """FastAPI dependency: resolves the logged-in merchant from the session cookie,
    or raises 401. Used to protect every merchant-only API route."""
    token = request.cookies.get(MERCHANT_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not logged in as a merchant")

    merchant_id = decode_merchant_session_token(token)
    if merchant_id is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid, please log in again")

    merchant = db.query(MerchantUser).filter(MerchantUser.id == merchant_id).first()
    if not merchant or not merchant.is_active:
        raise HTTPException(status_code=401, detail="Merchant account not found or inactive")

    return merchant


def get_current_merchant_or_none(request: Request, db: Session) -> MerchantUser | None:
    """Same lookup as get_current_merchant, but returns None instead of raising.

    Used by HTML PAGE routes (like the dashboard), which should redirect an
    unauthenticated visitor to the login page rather than show them a raw JSON
    401 error -- a browser page and an API endpoint need different failure
    behavior even though the underlying auth check is identical."""
    token = request.cookies.get(MERCHANT_COOKIE_NAME)
    if not token:
        return None

    merchant_id = decode_merchant_session_token(token)
    if merchant_id is None:
        return None

    merchant = db.query(MerchantUser).filter(MerchantUser.id == merchant_id).first()
    if not merchant or not merchant.is_active:
        return None

    return merchant


@router.post("/signup", response_model=MerchantOut)
def merchant_signup(payload: MerchantSignupRequest, response: Response, db: Session = Depends(get_db)):
    existing = db.query(MerchantUser).filter(MerchantUser.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    merchant = MerchantUser(
        email=payload.email,
        password_hash=hash_password(payload.password),
        store_name=payload.store_name,
    )
    db.add(merchant)
    db.commit()
    db.refresh(merchant)

    _set_merchant_cookie(response, merchant.id)
    return merchant


@router.post("/login", response_model=MerchantOut)
def merchant_login(payload: MerchantLoginRequest, response: Response, db: Session = Depends(get_db)):
    merchant = db.query(MerchantUser).filter(MerchantUser.email == payload.email).first()

    # Deliberately identical error for "no such email" and "wrong password" --
    # a different message for each would let an attacker enumerate valid emails.
    if not merchant or not verify_password(payload.password, merchant.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not merchant.is_active:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    _set_merchant_cookie(response, merchant.id)
    return merchant


@router.post("/logout")
def merchant_logout(response: Response):
    response.delete_cookie(MERCHANT_COOKIE_NAME)
    return {"status": "logged_out"}


@router.get("/me", response_model=MerchantOut)
def get_my_merchant_profile(current_merchant: MerchantUser = Depends(get_current_merchant)):
    return current_merchant