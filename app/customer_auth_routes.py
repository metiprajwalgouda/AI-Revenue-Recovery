"""
Customer signup/login/logout endpoints, and get_current_customer dependency.

Mirrors merchant_auth_routes.py structurally, but is a genuinely separate
system -- separate table, separate cookie, separate signing salt (see
app/auth.py's module docstring). A customer account can never be used to
access merchant-only routes and vice versa.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr, Field, ConfigDict
from typing import Optional

from app.db import get_db
from app.db_models import CustomerUser
from app.auth import (
    hash_password, verify_password,
    create_customer_session_token, decode_customer_session_token,
    CUSTOMER_COOKIE_NAME, SESSION_MAX_AGE_SECONDS,
)

router = APIRouter(prefix="/api/customer", tags=["customer-auth"])


class CustomerSignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=200)
    name: Optional[str] = None
    phone: Optional[str] = None


class CustomerLoginRequest(BaseModel):
    email: EmailStr
    password: str


class CustomerOut(BaseModel):
    id: int
    email: str
    name: Optional[str]
    phone: Optional[str]

    model_config = ConfigDict(from_attributes=True)


def _set_customer_cookie(response: Response, customer_id: int) -> None:
    token = create_customer_session_token(customer_id)
    response.set_cookie(
        key=CUSTOMER_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )


def get_current_customer(request: Request, db: Session = Depends(get_db)) -> CustomerUser:
    """FastAPI dependency: resolves the logged-in customer from the session cookie,
    or raises 401. Used to require a real account before checkout can start."""
    token = request.cookies.get(CUSTOMER_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Please log in to continue checkout")

    customer_id = decode_customer_session_token(token)
    if customer_id is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid, please log in again")

    customer = db.query(CustomerUser).filter(CustomerUser.id == customer_id).first()
    if not customer or not customer.is_active:
        raise HTTPException(status_code=401, detail="Account not found or inactive")

    return customer


@router.post("/signup", response_model=CustomerOut)
def customer_signup(payload: CustomerSignupRequest, response: Response, db: Session = Depends(get_db)):
    existing = db.query(CustomerUser).filter(CustomerUser.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    customer = CustomerUser(
        email=payload.email,
        password_hash=hash_password(payload.password),
        name=payload.name,
        phone=payload.phone,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)

    _set_customer_cookie(response, customer.id)
    return customer


@router.post("/login", response_model=CustomerOut)
def customer_login(payload: CustomerLoginRequest, response: Response, db: Session = Depends(get_db)):
    customer = db.query(CustomerUser).filter(CustomerUser.email == payload.email).first()

    if not customer or not verify_password(payload.password, customer.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not customer.is_active:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    _set_customer_cookie(response, customer.id)
    return customer


@router.post("/logout")
def customer_logout(response: Response):
    response.delete_cookie(CUSTOMER_COOKIE_NAME)
    return {"status": "logged_out"}


@router.get("/me", response_model=CustomerOut)
def get_my_customer_profile(current_customer: CustomerUser = Depends(get_current_customer)):
    return current_customer