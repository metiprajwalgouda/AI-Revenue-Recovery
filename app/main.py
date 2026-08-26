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

from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional

from app.db import init_db, get_db
from app.db_models import Product, CheckoutSession, SessionStatus
from app.razorpay_client import RazorpayRecoveryClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Checkout Recovery Storefront", lifespan=lifespan)


def get_razorpay_client() -> RazorpayRecoveryClient:
    """FastAPI dependency -- lets tests override this with a mock, same pattern as get_db."""
    return RazorpayRecoveryClient()


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
    name: str
    description: Optional[str]
    price: float
    stock: int
    image_url: Optional[str]
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


@app.post("/api/products", response_model=ProductOut)
def create_product(payload: ProductCreate, db: Session = Depends(get_db)):
    product = Product(**payload.model_dump())
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


@app.get("/api/products", response_model=list[ProductOut])
def list_products(include_inactive: bool = False, db: Session = Depends(get_db)):
    query = db.query(Product)
    if not include_inactive:
        query = query.filter(Product.is_active == True)  # noqa: E712
    return query.order_by(Product.created_at.desc()).all()


@app.get("/api/products/{product_id}", response_model=ProductOut)
def get_product(product_id: int, db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return product


@app.patch("/api/products/{product_id}", response_model=ProductOut)
def update_product(product_id: int, payload: ProductUpdate, db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(product, field, value)

    db.commit()
    db.refresh(product)
    return product


@app.delete("/api/products/{product_id}")
def delete_product(product_id: int, db: Session = Depends(get_db)):
    """Soft delete: sets is_active=False rather than removing the row."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
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
    customer_name: Optional[str] = None
    customer_email: str
    customer_phone: str
    cart_items: list[CartItem] = Field(..., min_length=1)


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
        customer_name=payload.customer_name,
        customer_email=payload.customer_email,
        customer_phone=payload.customer_phone,
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
):
    session = db.query(CheckoutSession).filter(CheckoutSession.event_id == payload.event_id).first()
    if not session:
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


@app.post("/api/checkout/abandon")
def abandon_checkout(payload: CheckoutAbandonRequest, db: Session = Depends(get_db)):
    """Called by the frontend when the customer explicitly closes the Razorpay popup
    without paying (Checkout.js 'ondismiss' callback). This is an IMMEDIATE, reliable
    abandonment signal -- much better than waiting for a timeout to guess. A background
    timeout sweep (Day 3) still catches cases where the browser/tab closes entirely
    without firing this callback (network drop, force-close, etc.)."""
    session = db.query(CheckoutSession).filter(CheckoutSession.event_id == payload.event_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Checkout session not found")

    if session.status == SessionStatus.COMPLETED:
        # Race condition guard: payment may have succeeded milliseconds before the
        # dismiss signal arrived (e.g. success callback + ondismiss both fire).
        # Never downgrade a completed order back to abandoned.
        return {"status": "already_completed", "event_id": session.event_id}

    session.status = SessionStatus.ABANDONED
    session.abandoned_at = datetime.now(timezone.utc)
    db.commit()

    return {"status": "abandoned", "event_id": session.event_id}