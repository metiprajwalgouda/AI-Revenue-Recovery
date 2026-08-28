"""
FastAPI application entrypoint.

Day 1 scope: product management API only (merchant CRUD).
Day 2 will add: storefront routes (browse products, cart, checkout).
Day 3 will add: abandonment detection + wiring into the recovery pipeline.
Day 4 will add: analytics dashboard routes.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import uuid

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional

from app.db import init_db, get_db
from app.db_models import Product, CheckoutSession, SessionStatus, RecoveryOutcomeRecord, MerchantUser, CustomerUser
from app.razorpay_client import RazorpayRecoveryClient, SimulatedRazorpayClient
from app.live_recovery import run_recovery_for_session
from app.merchant_auth_routes import router as merchant_auth_router, get_current_merchant
from app.customer_auth_routes import router as customer_auth_router, get_current_customer
import os
import anthropic


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Checkout Recovery Storefront", lifespan=lifespan)
app.include_router(merchant_auth_router)
app.include_router(customer_auth_router)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/", response_class=HTMLResponse)
def storefront_home(request: Request):
    return templates.TemplateResponse(request, "storefront.html", {})


@app.get("/account/login", response_class=HTMLResponse)
def customer_login_page(request: Request):
    return templates.TemplateResponse(request, "customer_login.html", {})


@app.get("/account/signup", response_class=HTMLResponse)
def customer_signup_page(request: Request):
    return templates.TemplateResponse(request, "customer_signup.html", {})


def get_razorpay_client() -> RazorpayRecoveryClient:
    """FastAPI dependency -- lets tests override this with a mock, same pattern as get_db."""
    return RazorpayRecoveryClient()


def get_recovery_razorpay_client():
    """Separate dependency for the RECOVERY action (payment link creation on abandonment),
    distinct from get_razorpay_client (used for the initial checkout order).

    Defaults to SimulatedRazorpayClient so casually testing abandonment on the live
    site doesn't burn through Razorpay's hard 30-payment-link test-mode cap (see
    CHALLENGES.md). Set RECOVERY_MODE=live in .env for your real demo recording,
    when you deliberately want a handful of genuine recovery links created."""
    if os.getenv("RECOVERY_MODE", "simulated") == "live":
        return RazorpayRecoveryClient()
    return SimulatedRazorpayClient()


def get_llm_client():
    """FastAPI dependency for the classifier's LLM fallback. Same client used by
    the batch pipeline -- if ANTHROPIC_API_KEY has no credit, classify() already
    degrades gracefully to 'unknown' + flag_for_manual_review (proven in the
    batch runs, see CHALLENGES.md), so this is safe to call even with no credits."""
    return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


class ProductCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    price: float = Field(..., gt=0)
    stock: int = Field(..., ge=0)
    image_url: Optional[str] = None


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = Field(None, gt=0)
    stock: Optional[int] = Field(None, ge=0)
    image_url: Optional[str] = None
    is_active: Optional[bool] = None


class ProductOut(BaseModel):
    id: int
    merchant_id: int
    name: str
    description: Optional[str]
    price: float
    stock: int
    image_url: Optional[str]
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


@app.post("/api/products", response_model=ProductOut)
def create_product(
    payload: ProductCreate,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant),
):
    product = Product(**payload.model_dump(), merchant_id=current_merchant.id)
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@app.get("/api/products", response_model=list[ProductOut])
def list_products(include_inactive: bool = False, db: Session = Depends(get_db)):
    """PUBLIC endpoint -- shows active products from ALL merchants, marketplace-style.
    No auth required: customers must be able to browse without logging in."""
    query = db.query(Product)
    if not include_inactive:
        query = query.filter(Product.is_active == True)  # noqa: E712
    return query.order_by(Product.created_at.desc()).all()


@app.get("/api/merchant/products", response_model=list[ProductOut])
def list_my_products(
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant),
):
    """PROTECTED endpoint for the merchant's own dashboard -- shows only THIS
    merchant's products, including inactive ones, unlike the public listing above."""
    return (
        db.query(Product)
        .filter(Product.merchant_id == current_merchant.id)
        .order_by(Product.created_at.desc())
        .all()
    )


@app.get("/api/products/{product_id}", response_model=ProductOut)
def get_product(product_id: int, db: Session = Depends(get_db)):
    """PUBLIC -- a customer viewing a product page doesn't need to be logged in."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@app.patch("/api/products/{product_id}", response_model=ProductOut)
def update_product(
    product_id: int,
    payload: ProductUpdate,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant),
):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product or product.merchant_id != current_merchant.id:
        # Same 404 whether the product doesn't exist OR belongs to someone else --
        # never reveal that a product ID exists under a different merchant's account.
        raise HTTPException(status_code=404, detail="Product not found")

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(product, field, value)

    db.commit()
    db.refresh(product)
    return product


@app.delete("/api/products/{product_id}")
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant),
):
    """Soft delete: sets is_active=False rather than removing the row."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product or product.merchant_id != current_merchant.id:
        raise HTTPException(status_code=404, detail="Product not found")

    product.is_active = False
    db.commit()
    return {"status": "deactivated", "product_id": product_id}


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- Checkout flow (customer side) ----------

class CartItem(BaseModel):
    product_id: int
    quantity: int = Field(..., gt=0)


class CheckoutStartRequest(BaseModel):
    cart_items: list[CartItem] = Field(..., min_length=1)
    # customer_name/email/phone REMOVED from the request body on purpose: they now
    # come from the logged-in CustomerUser account (get_current_customer), never
    # from client-supplied fields. A customer could otherwise type any email/phone
    # they wanted into the request, decoupling the order from who actually paid.


class CheckoutStartResponse(BaseModel):
    event_id: str
    order_id: str
    amount_paise: int
    razorpay_key_id: str
    cart_value: float


class CheckoutCompleteRequest(BaseModel):
    event_id: str
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class CheckoutAbandonRequest(BaseModel):
    event_id: str
    reason_hint: Optional[str] = None  # e.g. "user_closed_popup", not a guess at root cause,
                                        # just what the browser observed


@app.post("/api/checkout/start", response_model=CheckoutStartResponse)
def start_checkout(
    payload: CheckoutStartRequest,
    db: Session = Depends(get_db),
    razorpay_client: RazorpayRecoveryClient = Depends(get_razorpay_client),
    current_customer: CustomerUser = Depends(get_current_customer),
):
    # Validate products and compute the REAL server-side cart value.
    # NEVER trust a client-supplied total -- always recompute from the database.
    cart_value = 0.0
    for item in payload.cart_items:
        product = db.query(Product).filter(Product.id == item.product_id, Product.is_active == True).first()  # noqa: E712
        if not product:
            raise HTTPException(status_code=400, detail=f"Product {item.product_id} not found or inactive")
        if product.stock < item.quantity:
            raise HTTPException(status_code=400, detail=f"Insufficient stock for '{product.name}'")
        cart_value += product.price * item.quantity

    event_id = f"chk_{uuid.uuid4().hex[:12]}"

    order_result = razorpay_client.create_order(amount_rupees=cart_value, receipt=event_id)
    if not order_result.success:
        raise HTTPException(status_code=502, detail=f"Could not create payment order: {order_result.error_message}")

    session = CheckoutSession(
        event_id=event_id,
        customer_user_id=current_customer.id,
        customer_name=current_customer.name,
        customer_email=current_customer.email,
        customer_phone=current_customer.phone or "",
        cart_value=cart_value,
        cart_json=str([item.model_dump() for item in payload.cart_items]),
        status=SessionStatus.STARTED,
    )
    db.add(session)
    db.commit()

    return CheckoutStartResponse(
        event_id=event_id,
        order_id=order_result.order_id,
        amount_paise=int(round(cart_value * 100)),
        razorpay_key_id=razorpay_client.key_id,
        cart_value=cart_value,
    )


@app.post("/api/checkout/complete")
def complete_checkout(
    payload: CheckoutCompleteRequest,
    db: Session = Depends(get_db),
    razorpay_client: RazorpayRecoveryClient = Depends(get_razorpay_client),
    current_customer: CustomerUser = Depends(get_current_customer),
):
    session = db.query(CheckoutSession).filter(CheckoutSession.event_id == payload.event_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Checkout session not found")

    if session.customer_user_id != current_customer.id:
        # Same 404 whether the session doesn't exist OR belongs to a different
        # customer -- never confirm to a stranger that a given event_id is real.
        raise HTTPException(status_code=404, detail="Checkout session not found")

    # CRITICAL: verify the payment is genuine before marking anything as paid.
    # Without this, anyone could POST fake IDs here and mark an order complete for free.
    is_valid = razorpay_client.verify_payment_signature(
        order_id=payload.razorpay_order_id,
        payment_id=payload.razorpay_payment_id,
        signature=payload.razorpay_signature,
    )
    if not is_valid:
        raise HTTPException(status_code=400, detail="Payment signature verification failed")

    session.status = SessionStatus.COMPLETED
    session.completed_at = datetime.now(timezone.utc)
    session.razorpay_payment_id = payload.razorpay_payment_id
    db.commit()

    return {"status": "completed", "event_id": session.event_id}


class RecoveryOutcomeOut(BaseModel):
    predicted_reason: str
    confidence: float
    classification_method: str
    reasoning: Optional[str]
    action_taken: str
    action_success: bool
    amount_offered: Optional[float]
    error_message: Optional[str]

    model_config = ConfigDict(from_attributes=True)


class AbandonResponse(BaseModel):
    status: str
    event_id: str
    recovery: Optional[RecoveryOutcomeOut] = None


@app.post("/api/checkout/abandon", response_model=AbandonResponse)
def abandon_checkout(
    payload: CheckoutAbandonRequest,
    db: Session = Depends(get_db),
    recovery_razorpay_client=Depends(get_recovery_razorpay_client),
    llm_client=Depends(get_llm_client),
    current_customer: CustomerUser = Depends(get_current_customer),
):
    """Called by the frontend when the customer explicitly closes the Razorpay popup
    without paying (Checkout.js 'ondismiss' callback). This is an IMMEDIATE, reliable
    abandonment signal -- much better than waiting for a timeout to guess. A background
    timeout sweep (Day 3 continuation) still catches cases where the browser/tab closes
    entirely without firing this callback (network drop, force-close, etc.).

    On abandonment, immediately runs the SAME classify -> decide -> execute pipeline
    used by the batch runner (see app/live_recovery.py) -- a real customer's abandoned
    checkout gets a real (or simulated, depending on RECOVERY_MODE) recovery action
    taken within the same request, no separate batch job required."""
    session = db.query(CheckoutSession).filter(CheckoutSession.event_id == payload.event_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Checkout session not found")

    if session.customer_user_id != current_customer.id:
        raise HTTPException(status_code=404, detail="Checkout session not found")

    if session.status == SessionStatus.COMPLETED:
        # Race condition guard: payment may have succeeded milliseconds before the
        # dismiss signal arrived (e.g. success callback + ondismiss both fire).
        # Never downgrade a completed order back to abandoned.
        return AbandonResponse(status="already_completed", event_id=session.event_id)

    session.status = SessionStatus.ABANDONED
    session.abandoned_at = datetime.now(timezone.utc)
    db.commit()

    outcome_record = run_recovery_for_session(
        session=session, db=db, razorpay_client=recovery_razorpay_client, llm_client=llm_client
    )

    return AbandonResponse(
        status="abandoned",
        event_id=session.event_id,
        recovery=RecoveryOutcomeOut.model_validate(outcome_record),
    )