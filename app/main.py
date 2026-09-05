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
import json
import logging
import threading
import time

from fastapi import FastAPI, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from app.notification_service import manager as notif_manager
from app.db import init_db, get_db, SessionLocal
from app.db_models import (
    Product, CheckoutSession, SessionStatus, RecoveryOutcomeRecord, MerchantUser,
    CustomerUser, Coupon, CouponUsageLog, RecoveryCase, RecoveryActionLog, RecoveryScenario, CaseStatus,
    Invoice, InvoiceStatus,
)
from app.razorpay_client import (
    RazorpayRecoveryClient, SimulatedRazorpayClient,
    get_razorpay_client, get_recovery_razorpay_client,
)
from app.live_recovery import run_recovery_for_session
from app.merchant_auth_routes import router as merchant_auth_router, get_current_merchant, get_current_merchant_or_none
from app.customer_auth_routes import router as customer_auth_router, get_current_customer, get_current_customer_or_none
from app.merchant_extensions import router as merchant_extensions_router
from app.live_analytics import compute_live_analytics
from app.agent.receivables_dunning import reconcile_overdue_invoices, process_invoice_payment
import os
import asyncio

main_loop = None

# ---------------------------------------------------------------------------
# Receivables dunning background poller
# ---------------------------------------------------------------------------
# Scheduling approach chosen: stdlib background thread.
#
# Rationale: the project has no APScheduler/Celery in requirements.txt, and
# the existing lifespan/asyncio pattern is minimal. A daemon thread that
# sleeps in a loop is the simplest fit — zero new dependencies, survives
# uvicorn reload, and can be replaced with APScheduler or an external cron
# hitting /internal/reconcile at any point without touching this logic.
#
# Interval: DUNNING_POLL_INTERVAL_SECONDS (default 120 = 2 minutes).
# For demo fast-track (e.g. invoices due in 5 minutes), 2 minutes is responsive
# enough to watch the ladder advance live.

DUNNING_POLL_INTERVAL_SECONDS = int(os.getenv("DUNNING_POLL_INTERVAL_SECONDS", "120"))
_dunning_thread_stop = threading.Event()


def _dunning_worker() -> None:
    """Background thread: run reconcile_overdue_invoices() every N seconds."""
    _log = logging.getLogger("recovery_agent.dunning_scheduler")
    _log.info("Receivables dunning poller started (interval=%ds)", DUNNING_POLL_INTERVAL_SECONDS)
    while not _dunning_thread_stop.is_set():
        try:
            db = SessionLocal()
            try:
                result = reconcile_overdue_invoices(db)
                if any(v > 0 for v in result.values()):
                    _log.info("Dunning reconcile: %s", result)
            finally:
                db.close()
        except Exception as exc:
            _log.exception("Dunning reconcile error (will retry next interval): %s", exc)
        _dunning_thread_stop.wait(timeout=DUNNING_POLL_INTERVAL_SECONDS)
    _log.info("Receivables dunning poller stopped.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global main_loop
    main_loop = asyncio.get_running_loop()
    init_db()

    # Start dunning poller background thread
    _dunning_thread_stop.clear()
    _t = threading.Thread(target=_dunning_worker, name="dunning-poller", daemon=True)
    _t.start()

    yield

    # Signal poller to stop on shutdown (gives it up to 1s to notice)
    _dunning_thread_stop.set()


app = FastAPI(title="Checkout Recovery Storefront", lifespan=lifespan)
app.include_router(merchant_auth_router)
app.include_router(customer_auth_router)
app.include_router(merchant_extensions_router)


# ---------------------------------------------------------------------------
# Internal reconcile endpoint — manual trigger for cron / smoke testing
# ---------------------------------------------------------------------------
@app.post("/internal/reconcile")
def internal_reconcile(db: Session = Depends(get_db)):
    """
    Manual trigger for reconcile_overdue_invoices().

    Scheduling approach: this endpoint can be called by an external cron
    (e.g. crontab, GitHub Actions, curl) as a complement to the always-on
    background thread. Both paths are safe to run concurrently because
    reconcile_overdue_invoices() is fully idempotent (idempotency_key guards).

    No auth guard intentionally — designed for internal / server-side cron use
    only. In production, protect this with a reverse-proxy IP whitelist or
    a shared secret header check.
    """
    result = reconcile_overdue_invoices(db)
    return {"status": "ok", "reconcile_summary": result}


# ---------------------------------------------------------------------------
# Invoice payment complete endpoints (process_invoice_payment imported above)
# ---------------------------------------------------------------------------
class InvoicePaymentCompleteRequest(BaseModel):
    invoice_id: int
    razorpay_payment_id: str   # from Razorpay webhook or manual confirmation
    amount_paid: float


@app.post("/api/invoice-payment/complete")
def invoice_payment_complete(
    payload: InvoicePaymentCompleteRequest,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant),
):
    """
    Called when a merchant manually records or tests invoice payment completion.
    Strict merchant scoping: the invoice must belong to current_merchant.
    """
    invoice = db.query(Invoice).filter(
        Invoice.id == payload.invoice_id,
        Invoice.merchant_id == current_merchant.id,
    ).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if invoice.status in (InvoiceStatus.PAID, InvoiceStatus.CANCELLED):
        return {"status": "already_resolved", "invoice_status": invoice.status.value}

    case = process_invoice_payment(
        db=db,
        invoice=invoice,
        razorpay_payment_id=payload.razorpay_payment_id,
        amount_paid=payload.amount_paid,
        channel="manual_api",
    )

    return {
        "status": "success",
        "invoice_status": "paid",
        "case_status": case.status.value if case else None,
    }


# ---------------------------------------------------------------------------
# Real Razorpay Webhook Endpoint
# ---------------------------------------------------------------------------
@app.post("/api/webhooks/razorpay")
async def razorpay_webhook_endpoint(
    request: Request,
    db: Session = Depends(get_db),
    razorpay_client=Depends(get_razorpay_client),
):
    """
    Real Razorpay Webhook Endpoint.

    Handles webhook events from Razorpay (test or live mode):
      - payment_link.paid
      - payment.captured
      - order.paid
      - invoice.paid

    Security:
      - Verifies the X-Razorpay-Signature header against the raw request body
        using HMAC SHA256 (via razorpay_client.verify_webhook_signature).

    Recovery action:
      - Resolves the Invoice and linked RecoveryCase.
      - Automatically transitions Invoice to PAID and Case to RECOVERED.
      - Writes an idempotent RecoveryActionLog audit record.
    """
    body_bytes = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    # 1. Verify webhook signature if header is supplied
    if signature:
        is_valid = razorpay_client.verify_webhook_signature(body_bytes, signature)
        if not is_valid:
            logging.getLogger("recovery_agent.webhook").warning("Razorpay webhook signature verification FAILED.")
            raise HTTPException(status_code=400, detail="Invalid webhook signature")

    # 2. Parse JSON payload
    try:
        data = json.loads(body_bytes.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {exc}")

    event = data.get("event", "")
    payload_data = data.get("payload", {})
    logging.getLogger("recovery_agent.webhook").info("Razorpay webhook received: event=%s", event)

    resolved_invoice = None
    payment_id = None
    amount_paid = 0.0

    plink_entity = payload_data.get("payment_link", {}).get("entity", {})
    payment_entity = payload_data.get("payment", {}).get("entity", {})

    if payment_entity:
        payment_id = payment_entity.get("id")
        amount_paise = payment_entity.get("amount", 0)
        amount_paid = float(amount_paise) / 100.0 if amount_paise else 0.0

    if not payment_id and plink_entity:
        payment_id = plink_entity.get("id")
        amount_paise = plink_entity.get("amount_paid") or plink_entity.get("amount", 0)
        amount_paid = float(amount_paise) / 100.0 if amount_paise else 0.0

    # Lookup invoice by payment link ID
    if plink_entity.get("id"):
        plink_id = plink_entity.get("id")
        resolved_invoice = db.query(Invoice).filter(Invoice.razorpay_invoice_id == plink_id).first()

    # Lookup by notes
    if not resolved_invoice:
        notes = plink_entity.get("notes", {}) or payment_entity.get("notes", {}) or {}
        if notes.get("invoice_id"):
            try:
                inv_id = int(notes.get("invoice_id"))
                resolved_invoice = db.query(Invoice).filter(Invoice.id == inv_id).first()
            except Exception:
                pass
        if not resolved_invoice and notes.get("invoice_number"):
            inv_num = notes.get("invoice_number")
            resolved_invoice = db.query(Invoice).filter(Invoice.invoice_number == inv_num).first()

    # Lookup by reference_id
    if not resolved_invoice and plink_entity.get("reference_id"):
        ref_id = plink_entity.get("reference_id")
        if ref_id.startswith("inv_"):
            parts = ref_id.split("_")
            if len(parts) >= 2:
                possible_num = "_".join(parts[1:-1]) if len(parts) > 2 else parts[1]
                resolved_invoice = db.query(Invoice).filter(Invoice.invoice_number == possible_num).first()
                if not resolved_invoice and parts[1].isdigit():
                    resolved_invoice = db.query(Invoice).filter(Invoice.id == int(parts[1])).first()

    if resolved_invoice:
        if amount_paid <= 0:
            amount_paid = resolved_invoice.amount
        if not payment_id:
            payment_id = f"pay_{uuid.uuid4().hex[:10]}"

        case = process_invoice_payment(
            db=db,
            invoice=resolved_invoice,
            razorpay_payment_id=payment_id,
            amount_paid=amount_paid,
            channel="razorpay_webhook",
        )
        return {
            "status": "success",
            "event": event,
            "invoice_id": resolved_invoice.id,
            "invoice_number": resolved_invoice.invoice_number,
            "invoice_status": resolved_invoice.status.value,
            "case_status": case.status.value if case else None,
        }

    return {"status": "ignored", "event": event, "reason": "No matching invoice found for webhook event"}


# ---------------------------------------------------------------------------
# Customer Confirmation Callback Page
# ---------------------------------------------------------------------------
@app.get("/invoice/{invoice_number}/confirmation", response_class=HTMLResponse)
def invoice_payment_confirmation_page(
    invoice_number: str,
    request: Request,
    db: Session = Depends(get_db),
    razorpay_payment_id: Optional[str] = None,
    razorpay_payment_link_id: Optional[str] = None,
    razorpay_payment_link_status: Optional[str] = None,
):
    """
    Customer-facing confirmation page rendered after completing payment on a Razorpay Payment Link.
    """
    invoice = db.query(Invoice).filter(Invoice.invoice_number == invoice_number).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if (razorpay_payment_link_status == "paid" or razorpay_payment_id) and invoice.status != InvoiceStatus.PAID:
        pay_id = razorpay_payment_id or f"pay_link_{razorpay_payment_link_id or uuid.uuid4().hex[:6]}"
        process_invoice_payment(
            db=db,
            invoice=invoice,
            razorpay_payment_id=pay_id,
            amount_paid=invoice.amount,
            channel="customer_redirect_callback",
        )

    merchant = invoice.merchant
    customer = invoice.customer

    return templates.TemplateResponse(
        request,
        "invoice_confirmation.html",
        {
            "store_name": merchant.store_name if merchant else "Store",
            "invoice": invoice,
            "customer": customer,
            "payment_id": razorpay_payment_id or (f"Ref: {invoice.razorpay_invoice_id}" if invoice.razorpay_invoice_id else "Confirmed"),
            "is_paid": (invoice.status == InvoiceStatus.PAID),
        },
    )


@app.websocket("/ws/notifications/{customer_id}")
async def websocket_endpoint(websocket: WebSocket, customer_id: int):
    await notif_manager.connect(customer_id, websocket)
    try:
        while True:
            # We don't really expect messages from client, just ping/pong to keep alive
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        notif_manager.disconnect(customer_id, websocket)

@app.get("/api/customer/notifications")
def get_notifications(db: Session = Depends(get_db), current_customer: CustomerUser = Depends(get_current_customer)):
    from app.db_models import InAppNotification
    notifs = db.query(InAppNotification).filter(InAppNotification.customer_user_id == current_customer.id).order_by(InAppNotification.created_at.desc()).all()
    return [{
        "id": n.id,
        "title": n.title,
        "message": n.message,
        "action_url": n.action_url,
        "is_read": n.is_read,
        "created_at": n.created_at.isoformat() if n.created_at else None
    } for n in notifs]

@app.post("/api/customer/notifications/{notif_id}/read")
def mark_notification_read(notif_id: int, db: Session = Depends(get_db), current_customer: CustomerUser = Depends(get_current_customer)):
    from app.db_models import InAppNotification
    notif = db.query(InAppNotification).filter(InAppNotification.id == notif_id, InAppNotification.customer_user_id == current_customer.id).first()
    if notif:
        notif.is_read = True
        db.commit()
    return {"status": "success"}

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/", response_class=HTMLResponse)
def storefront_home(request: Request, db: Session = Depends(get_db)):
    customer = get_current_customer_or_none(request, db)
    return templates.TemplateResponse(request, "storefront.html", {
        "customer": customer
    })


@app.get("/account/login", response_class=HTMLResponse)
def customer_login_page(request: Request):
    return templates.TemplateResponse(request, "customer_login.html", {})


@app.get("/account/signup", response_class=HTMLResponse)
def customer_signup_page(request: Request):
    return templates.TemplateResponse(request, "customer_signup.html", {})

from fastapi.responses import Response

@app.api_route("/api/twilio-twiml", methods=["GET", "POST"], response_class=Response)
def twilio_twiml_endpoint(
    event_id: Optional[str] = None,
    coupon_code: Optional[str] = None,
    invoice_number: Optional[str] = None,
    days_overdue: Optional[int] = 0,
    amount: Optional[str] = None,
    customer_name: Optional[str] = None,
    db: Session = Depends(get_db)
):
    generic_twiml = '<?xml version="1.0" encoding="UTF-8"?><Response><Say>Hi, you have an order waiting. Check your email or SMS for the link to complete your purchase.</Say></Response>'

    # 1. If this is an invoice call
    if invoice_number:
        name = customer_name or "there"
        amt_str = f"{float(amount):.2f}" if amount else "the outstanding balance"
        twiml_msg = (
            f"Hi {name}, this is an urgent reminder regarding Invoice {invoice_number} for {amt_str} rupees, "
            f"which is currently {days_overdue or 0} days overdue. Please check your email or SMS for your payment link to complete payment. Thank you."
        )
        twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{twiml_msg}</Say></Response>'
        return Response(content=twiml, media_type="text/xml")

    # 2. Checkout abandonment or payment failure call
    if not event_id:
        return Response(content=generic_twiml, media_type="text/xml")

    session = db.query(CheckoutSession).filter(CheckoutSession.event_id == event_id).first()
    if not session:
        return Response(content=generic_twiml, media_type="text/xml")

    from app.db_models import RecoveryOutcomeRecord
    from app.agent.recovery_actions import is_discount_allowed

    outcome = db.query(RecoveryOutcomeRecord).filter(RecoveryOutcomeRecord.session_id == session.id).first()
    mention_discount = False
    if outcome and is_discount_allowed(outcome.predicted_reason):
        mention_discount = True

    name = session.customer_name or "there"
    amount = session.cart_value or "0"
    
    twiml_msg = f"Hi {name}, you have an order of {amount} rupees waiting. "
    if mention_discount and coupon_code:
        twiml_msg += f"Check your email or SMS for the link to complete your purchase using coupon code {coupon_code}."
    else:
        twiml_msg += "Check your email or SMS for the link to complete your purchase."

    twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{twiml_msg}</Say></Response>'
    return Response(content=twiml, media_type="text/xml")

@app.get("/cart", response_class=HTMLResponse)
def cart_page(request: Request, resume: Optional[str] = None, db: Session = Depends(get_db)):
    resume_cart_json = None
    if resume:
        session = db.query(CheckoutSession).filter(CheckoutSession.event_id == resume).first()
        if session and session.cart_json:
            try:
                raw_items = json.loads(session.cart_json)
                hydrated = []
                for item in raw_items:
                    product = db.query(Product).filter(Product.id == item.get("product_id")).first()
                    if product:
                        hydrated.append({
                            "product_id": product.id,
                            "name": product.name,
                            "price": product.price,
                            "quantity": item.get("quantity", 1)
                        })
                resume_cart_json = json.dumps(hydrated)
            except Exception:
                pass
            
    return templates.TemplateResponse(request, "cart.html", {"resume_cart_json": resume_cart_json, "resume_event_id": resume})

@app.get("/order-confirmation", response_class=HTMLResponse)
def order_confirmation_page(request: Request, event_id: str, db: Session = Depends(get_db)):
    session = db.query(CheckoutSession).filter(CheckoutSession.event_id == event_id).first()
    if not session:
        return RedirectResponse(url="/")
        
    cart_items = []
    if session.cart_json:
        try:
            raw_items = json.loads(session.cart_json)
            for item in raw_items:
                product = db.query(Product).filter(Product.id == item.get("product_id")).first()
                cart_items.append({
                    "name": product.name if product else f"Product #{item.get('product_id')}",
                    "price": product.price if product else 0.0,
                    "quantity": item.get("quantity", 1)
                })
        except:
            pass
            
    coupon = None
    if session.applied_coupon_id:
        coupon = db.query(Coupon).filter(Coupon.id == session.applied_coupon_id).first()

    return templates.TemplateResponse(request, "order_confirmation.html", {
        "session": session,
        "cart_items": cart_items,
        "coupon": coupon
    })

@app.get("/products", response_class=HTMLResponse)
@app.get("/products/", response_class=HTMLResponse)
@app.get("/product", response_class=HTMLResponse)
@app.get("/product/", response_class=HTMLResponse)
def products_page_redirect():
    return RedirectResponse(url="/#product-grid", status_code=302)


@app.get("/product/{product_id}", response_class=HTMLResponse)
def product_detail_page(request: Request, product_id: int, db: Session = Depends(get_db)):
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        return RedirectResponse(url="/", status_code=302)
    
    merchant = db.query(MerchantUser).filter(MerchantUser.id == product.merchant_id).first()
    related_products = db.query(Product).filter(
        Product.id != product.id,
        Product.is_active == True
    ).limit(4).all()

    orig_mrp = round(product.price * 1.25, 2)
    save_amt = round(orig_mrp - product.price, 2)
    discount_pct = 20

    return templates.TemplateResponse(request, "product_detail.html", {
        "product": product,
        "merchant": merchant,
        "related_products": related_products,
        "orig_mrp": orig_mrp,
        "save_amt": save_amt,
        "discount_pct": discount_pct
    })



@app.get("/merchant/broadcasts", response_class=HTMLResponse)
def merchant_broadcasts_page(request: Request):
    return RedirectResponse(url="/merchant/customers", status_code=302)

@app.get("/merchant/coupons", response_class=HTMLResponse)
@app.get("/merchant/coupon-recovery", response_class=HTMLResponse)
def merchant_coupon_recovery_page(request: Request):
    return RedirectResponse(url="/merchant/settings#coupon-usage", status_code=302)

@app.get("/merchant/settings", response_class=HTMLResponse)
def merchant_settings_page(request: Request):
    return templates.TemplateResponse(request, "merchant_settings.html", {"active_section": "settings"})

class MerchantSettingsUpdate(BaseModel):
    contact_email: Optional[str]
    phone: Optional[str]
    business_category: Optional[str]
    min_discount_pct: int
    max_discount_pct: int
    max_recovery_attempts: int
    high_value_threshold_amount: float
    auto_call_high_priority: bool = False
    auto_email_high_priority: bool = False
    automated_recovery_message: Optional[str] = None

@app.get("/api/merchant/settings")
def get_merchant_settings(db: Session = Depends(get_db), current_merchant: MerchantUser = Depends(get_current_merchant)):
    return {
        "contact_email": current_merchant.contact_email,
        "phone": current_merchant.phone,
        "business_category": current_merchant.business_category,
        "min_discount_pct": current_merchant.min_discount_pct,
        "max_discount_pct": current_merchant.max_discount_pct,
        "max_recovery_attempts": current_merchant.max_recovery_attempts,
        "high_value_threshold_amount": current_merchant.high_value_threshold_amount,
        "auto_call_high_priority": current_merchant.auto_call_high_priority,
        "auto_email_high_priority": current_merchant.auto_email_high_priority,
        "automated_recovery_message": current_merchant.automated_recovery_message,
    }

@app.post("/api/merchant/settings")
def update_merchant_settings(payload: MerchantSettingsUpdate, db: Session = Depends(get_db), current_merchant: MerchantUser = Depends(get_current_merchant)):
    if payload.min_discount_pct > payload.max_discount_pct:
        raise HTTPException(status_code=400, detail="min_discount_pct cannot be greater than max_discount_pct")
    current_merchant.contact_email = payload.contact_email
    current_merchant.phone = payload.phone
    current_merchant.business_category = payload.business_category
    current_merchant.min_discount_pct = payload.min_discount_pct
    current_merchant.max_discount_pct = payload.max_discount_pct
    current_merchant.max_recovery_attempts = payload.max_recovery_attempts
    current_merchant.high_value_threshold_amount = payload.high_value_threshold_amount
    current_merchant.auto_call_high_priority = payload.auto_call_high_priority
    current_merchant.auto_email_high_priority = payload.auto_email_high_priority
    current_merchant.auto_call_high_priority = payload.auto_call_high_priority
    current_merchant.automated_recovery_message = payload.automated_recovery_message
    db.commit()
    return {"status": "success"}

@app.get("/merchant/customers", response_class=HTMLResponse)
def merchant_customers_page(request: Request):
    return templates.TemplateResponse(request, "merchant_customers.html", {})

@app.get("/merchant/revenue-intelligence", response_class=HTMLResponse)
def merchant_roi_dashboard_page(request: Request):
    return templates.TemplateResponse(request, "merchant_roi_dashboard.html", {})

@app.get("/merchant/batch-report")
def merchant_batch_report_page():
    return RedirectResponse(url="/merchant/revenue-intelligence", status_code=302)


@app.get("/merchant/orders", response_class=HTMLResponse)
def merchant_orders_page(request: Request):
    return templates.TemplateResponse(request, "merchant_orders.html", {})

@app.get("/merchant/priority", response_class=HTMLResponse)
def merchant_priority_page(request: Request, db: Session = Depends(get_db)):
    merchant = get_current_merchant_or_none(request, db)
    if merchant is None:
        return RedirectResponse(url="/merchant/login")
    return templates.TemplateResponse(request, "merchant_priority.html", {"store_name": merchant.store_name, "active_section": "priority"})

@app.get("/api/merchant/priority")
def get_priority_orders(
    scenario: Optional[str] = None,
    type: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    """Returns human follow-ups queue combining:
    1. RecoveryActionLog rows with outcome="pending_approval" across ALL scenarios (awaiting merchant discount/coupon approval).
    2. RecoveryCase rows with escalated_to_human=True across ALL scenarios (payment_failure, checkout_abandonment, overdue_receivable).
    3. High-priority abandoned CheckoutSession rows (VIP threshold) not yet resolved.
    """
    result = []
    handled_session_ids = set()

    # 1. Query pending approval RecoveryActionLog rows for this merchant
    pending_logs = (
        db.query(RecoveryActionLog)
        .join(RecoveryCase, RecoveryActionLog.case_id == RecoveryCase.id)
        .filter(
            RecoveryActionLog.outcome == "pending_approval",
            RecoveryCase.merchant_id == current_merchant.id,
        )
        .order_by(RecoveryActionLog.created_at.desc())
        .all()
    )

    for l in pending_logs:
        c = l.case
        scenario_val = c.scenario.value if hasattr(c.scenario, "value") else str(c.scenario)
        customer_name = (c.customer.name if c.customer else None) or (c.checkout_session.customer_name if c.checkout_session else None) or (c.invoice.customer.name if c.invoice and c.invoice.customer else None) or "Customer"
        customer_email = (c.customer.email if c.customer else None) or (c.checkout_session.customer_email if c.checkout_session else None) or (c.invoice.customer.email if c.invoice and c.invoice.customer else None) or "N/A"
        customer_phone = (c.customer.phone if c.customer else None) or (c.checkout_session.customer_phone if c.checkout_session else None) or (c.invoice.customer.phone if c.invoice and c.invoice.customer else None) or "N/A"

        result.append({
            "id": l.id,
            "target_id": f"log_{l.id}",
            "target_type": "log",
            "item_type": "pending_approval",
            "log_id": l.id,
            "case_id": c.id,
            "session_id": c.checkout_session_id,
            "invoice_id": c.invoice_id,
            "event_id": c.checkout_session.event_id if c.checkout_session else f"case_{c.id}",
            "scenario": scenario_val,
            "customer_name": customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
            "cart_value": c.amount_at_risk,
            "amount_at_risk": c.amount_at_risk,
            "amount_offered": l.amount_offered,
            "coupon_code": l.coupon_code,
            "ladder_step": l.ladder_step,
            "action_type": l.action_type,
            "started_at": l.created_at.isoformat() if l.created_at else None,
            "created_at": l.created_at.isoformat() if l.created_at else None,
            "reason": l.reason or "Coupon/Discount offer pending merchant approval",
            "escalation_reason": "Pending Merchant Approval",
            "status": "pending_approval",
            "is_escalated_case": False,
            "is_pending_approval": True,
            "requires_human_approval": l.requires_human_approval,
        })

    # 2. Query all escalated RecoveryCase rows for this merchant (strict — no cross-merchant leakage)
    case_query = db.query(RecoveryCase).filter(
        RecoveryCase.escalated_to_human == True,
        RecoveryCase.merchant_id == current_merchant.id,
    )
    escalated_cases = case_query.order_by(RecoveryCase.created_at.desc()).all()

    for c in escalated_cases:
        if c.checkout_session_id:
            handled_session_ids.add(c.checkout_session_id)
            outcome = db.query(RecoveryOutcomeRecord).filter(RecoveryOutcomeRecord.session_id == c.checkout_session_id).first()
            if outcome and outcome.action_taken == "manual_followup":
                continue

        scenario_val = c.scenario.value if hasattr(c.scenario, "value") else str(c.scenario)
        customer_name = (c.customer.name if c.customer else None) or (c.checkout_session.customer_name if c.checkout_session else None) or (c.invoice.customer.name if c.invoice and c.invoice.customer else None) or "Customer"
        customer_email = (c.customer.email if c.customer else None) or (c.checkout_session.customer_email if c.checkout_session else None) or (c.invoice.customer.email if c.invoice and c.invoice.customer else None) or "N/A"
        customer_phone = (c.customer.phone if c.customer else None) or (c.checkout_session.customer_phone if c.checkout_session else None) or (c.invoice.customer.phone if c.invoice and c.invoice.customer else None) or "N/A"

        status_val = c.status.value if hasattr(c.status, "value") else str(c.status)

        result.append({
            "id": c.checkout_session_id or c.id,
            "target_id": f"case_{c.id}",
            "target_type": "case",
            "item_type": "escalated_case",
            "case_id": c.id,
            "session_id": c.checkout_session_id,
            "invoice_id": c.invoice_id,
            "event_id": c.checkout_session.event_id if c.checkout_session else f"case_{c.id}",
            "scenario": scenario_val,
            "customer_name": customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
            "cart_value": c.amount_at_risk,
            "amount_at_risk": c.amount_at_risk,
            "amount_recovered": c.amount_recovered,
            "status": status_val,
            "started_at": c.created_at.isoformat() if c.created_at else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "reason": c.classification or c.escalation_reason or "escalated_to_human",
            "escalation_reason": c.escalation_reason or (c.classification or "Escalated to human"),
            "is_escalated_case": True,
            "is_pending_approval": False,
        })

    # 3. Query high-priority abandoned CheckoutSession rows
    sessions = db.query(CheckoutSession).filter(
        CheckoutSession.status == SessionStatus.ABANDONED,
        CheckoutSession.is_high_priority == True
    ).order_by(CheckoutSession.started_at.desc()).all()

    for s in sessions:
        if s.id in handled_session_ids:
            continue
        outcome = db.query(RecoveryOutcomeRecord).filter(RecoveryOutcomeRecord.session_id == s.id).first()
        if outcome and outcome.action_taken == "manual_followup":
            continue

        result.append({
            "id": s.id,
            "target_id": f"session_{s.id}",
            "target_type": "session",
            "item_type": "escalated_case",
            "case_id": None,
            "session_id": s.id,
            "event_id": s.event_id,
            "scenario": "checkout_abandonment",
            "customer_name": s.customer_name or "Guest",
            "customer_email": s.customer_email,
            "customer_phone": s.customer_phone or "N/A",
            "cart_value": s.cart_value,
            "amount_at_risk": s.cart_value,
            "amount_recovered": 0.0,
            "status": "abandoned",
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "created_at": s.started_at.isoformat() if s.started_at else None,
            "reason": outcome.predicted_reason if outcome else "high_value_abandonment",
            "escalation_reason": "High-value checkout abandonment (VIP threshold)",
            "is_escalated_case": False,
            "is_pending_approval": False,
        })

    # Optional server-side filtering
    if scenario and scenario != "all":
        result = [r for r in result if r.get("scenario", "").lower() == scenario.lower()]
    if type and type != "all":
        result = [r for r in result if r.get("item_type", "").lower() == type.lower()]
    if status and status != "all":
        result = [r for r in result if r.get("status", "").lower() == status.lower()]

    return result


def _resolve_priority_target(target_id: str, db: Session):
    """Resolves session and case deterministically from a target_id string.
    Supports prefix-based IDs ('case_123', 'session_123') to eliminate ID collision,
    while falling back gracefully for raw integer IDs."""
    session = None
    case = None

    if target_id.startswith("case_"):
        case_id = int(target_id.replace("case_", ""))
        case = db.query(RecoveryCase).filter(RecoveryCase.id == case_id).first()
        if case and case.checkout_session:
            session = case.checkout_session
    elif target_id.startswith("session_"):
        sess_id = int(target_id.replace("session_", ""))
        session = db.query(CheckoutSession).filter(CheckoutSession.id == sess_id).first()
        if session:
            case = db.query(RecoveryCase).filter(RecoveryCase.checkout_session_id == session.id).first()
    else:
        # Backward compatibility for plain numeric IDs:
        # Check RecoveryCase first if it's an escalated case, otherwise CheckoutSession
        numeric_id = int(target_id)
        case = db.query(RecoveryCase).filter(RecoveryCase.id == numeric_id).first()
        if case:
            if case.checkout_session:
                session = case.checkout_session
        else:
            session = db.query(CheckoutSession).filter(CheckoutSession.id == numeric_id).first()
            if session:
                case = db.query(RecoveryCase).filter(RecoveryCase.checkout_session_id == session.id).first()

    return session, case


@app.post("/api/merchant/priority/{target_id}/call")
def priority_call_customer(
    target_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    session, case = _resolve_priority_target(target_id, db)
    if not session and not case:
        raise HTTPException(status_code=404, detail="Target not found")
            
    customer_phone = (
        (session.customer_phone if session else None) or
        (case.customer.phone if case and case.customer else None) or
        (case.invoice.customer.phone if case and case.invoice and case.invoice.customer else None)
    )
    customer_name = (
        (session.customer_name or session.customer_email) if session else
        (case.customer.name if case and case.customer else (case.invoice.customer.name if case and case.invoice and case.invoice.customer else "Customer"))
    )
    cart_value = session.cart_value if session else (case.amount_at_risk if case else 0.0)

    if not customer_phone or customer_phone == "N/A":
        raise HTTPException(status_code=400, detail="Customer has no phone number on file")
        
    from app.voice_service import make_recovery_call, make_invoice_overdue_call
    from app.sms_service import send_recovery_sms
    from app.config import get_base_url
    
    base_url = get_base_url(request)
    
    # 1. Check if this is an Overdue Receivable scenario
    is_receivable = case and (case.scenario == RecoveryScenario.OVERDUE_RECEIVABLE or case.invoice_id is not None)
    
    if is_receivable and case and case.invoice:
        inv = case.invoice
        invoice_num = inv.invoice_number
        inv_amount = inv.amount
        
        # Calculate days overdue
        days_overdue = 0
        if inv.due_date:
            due_dt = inv.due_date if inv.due_date.tzinfo else inv.due_date.replace(tzinfo=timezone.utc)
            days_overdue = max(0, (datetime.now(timezone.utc) - due_dt).days)
            
        pay_url = inv.payment_link_url or f"{base_url}/invoice-pay/{inv.id}"
        
        # 1a. Voice call for overdue invoice
        call_res = make_invoice_overdue_call(customer_phone, customer_name, invoice_num, inv_amount, days_overdue, pay_url)
        if call_res["status"] == "skipped_no_key":
            return {"status": "skipped_no_key"}
        if call_res["status"] == "failed":
            return {"status": "failed", "error": call_res.get("error")}
            
        # 1b. SMS Follow-up with invoice payment link
        sms_message = f"Hi {customer_name}, here is the payment link for Invoice {invoice_num} (Rs. {inv_amount:.2f}, {days_overdue}d overdue): {pay_url}"
        sms_res = send_recovery_sms(customer_phone, sms_message)
        
        # 1c. Log to audit trail
        step = (case.ladder_step or 0) + 1
        case.ladder_step = step
        idempotency_key = f"{case.id}:{step}:manual_voice_call_{uuid.uuid4().hex[:6]}"
        action_log = RecoveryActionLog(
            case_id=case.id,
            idempotency_key=idempotency_key,
            ladder_step=step,
            action_type="manual_voice_call",
            reason=f"Manual voice call initiated to {customer_phone} regarding overdue Invoice {invoice_num} ({days_overdue} days overdue, Rs. {inv_amount:.2f}).",
            guardrail_checks=json.dumps({
                "channel": "voice_call",
                "manual": True,
                "scenario": "overdue_receivable",
                "invoice_number": invoice_num,
                "days_overdue": days_overdue
            }),
            outcome="sent",
            amount_offered=inv_amount,
            requires_human_approval=False,
            approved_by=current_merchant.id,
            approved_at=datetime.now(timezone.utc),
        )
        db.add(action_log)
        case.contact_touches = (case.contact_touches or 0) + 1
        case.last_action_at = datetime.now(timezone.utc)
        
        db.commit()
        return {"status": "success", "call_sid": call_res.get("sid"), "sms_sid": sms_res.get("sid")}

    # 2. Checkout Abandonment or Payment Failure scenario
    event_id = session.event_id if session else (f"case_{case.id}" if case else "recovery")
    resume_url = f"{base_url}/cart?resume={event_id}"
    
    # 2a. Voice call
    call_res = make_recovery_call(customer_phone, customer_name, cart_value, resume_url)
    if call_res["status"] == "skipped_no_key":
        return {"status": "skipped_no_key"}
    if call_res["status"] == "failed":
        return {"status": "failed", "error": call_res.get("error")}
        
    # 2b. SMS Follow-up
    sms_message = f"Hi {customer_name}, here is the link to complete your checkout: {resume_url}"
    sms_res = send_recovery_sms(customer_phone, sms_message)
    
    # 2c. Log to audit trail
    if session:
        outcome = db.query(RecoveryOutcomeRecord).filter(RecoveryOutcomeRecord.session_id == session.id).first()
        if not outcome:
            outcome = RecoveryOutcomeRecord(
                session_id=session.id,
                predicted_reason="unknown",
                confidence=1.0,
                classification_method="manual",
                reasoning="Voice call escalated manually by merchant",
                action_taken="voice_call",
                action_success=True,
                amount_offered=0.0,
                delivery_status=call_res["status"]
            )
            db.add(outcome)
        else:
            outcome.action_taken = "voice_call"
            outcome.action_success = True
            outcome.delivery_status = call_res["status"]
            outcome.reasoning = "Voice call escalated manually by merchant"

    if case:
        step = (case.ladder_step or 0) + 1
        case.ladder_step = step
        idempotency_key = f"{case.id}:{step}:manual_call_{uuid.uuid4().hex[:6]}"
        action_log = RecoveryActionLog(
            case_id=case.id,
            idempotency_key=idempotency_key,
            ladder_step=step,
            action_type="manual_call",
            reason=f"Manual voice call initiated to {customer_phone}.",
            guardrail_checks=json.dumps({"channel": "call", "manual": True}),
            outcome="sent",
            amount_offered=cart_value,
            requires_human_approval=False,
            approved_by=current_merchant.id,
            approved_at=datetime.now(timezone.utc),
        )
        db.add(action_log)
        case.contact_touches = (case.contact_touches or 0) + 1
        case.last_action_at = datetime.now(timezone.utc)
        
    db.commit()
    return {"status": "success", "call_sid": call_res.get("sid"), "sms_sid": sms_res.get("sid")}


@app.post("/api/merchant/priority/{target_id}/contact")
def mark_priority_contacted(
    target_id: str,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    session, case = _resolve_priority_target(target_id, db)

    if not session and not case:
        raise HTTPException(status_code=404, detail="Session or recovery case not found")

    if case and case.merchant_id != current_merchant.id:
        raise HTTPException(status_code=403, detail="You do not have permission to modify this case.")
        
    if session:
        outcome = db.query(RecoveryOutcomeRecord).filter(RecoveryOutcomeRecord.session_id == session.id).first()
        if not outcome:
            outcome = RecoveryOutcomeRecord(
                session_id=session.id,
                predicted_reason="unknown",
                confidence=1.0,
                classification_method="manual",
                reasoning="Manually tracked",
                action_taken="manual_followup",
                action_success=True,
                delivery_status="sent"
            )
            db.add(outcome)
        else:
            outcome.action_taken = "manual_followup"

    if case:
        case.escalated_to_human = False
        case.status = CaseStatus.INTERVENING
        case.last_action_at = datetime.now(timezone.utc)
        
        step = (case.ladder_step or 0) + 1
        case.ladder_step = step
        idempotency_key = f"{case.id}:{step}:manual_contact_{uuid.uuid4().hex[:6]}"
        action_log = RecoveryActionLog(
            case_id=case.id,
            idempotency_key=idempotency_key,
            ladder_step=step,
            action_type="manual_contact",
            reason="Merchant resolved escalation via manual contact.",
            guardrail_checks=json.dumps({"manual_contact": True}),
            outcome="sent",
            amount_offered=case.amount_at_risk,
            requires_human_approval=False,
            approved_by=current_merchant.id,
            approved_at=datetime.now(timezone.utc),
        )
        db.add(action_log)
        
    db.commit()
    return {"status": "success"}


class MarkLostRequest(BaseModel):
    reason: Optional[str] = None


@app.post("/api/merchant/priority/{target_id}/lost")
def mark_priority_lost(
    target_id: str,
    payload: Optional[MarkLostRequest] = None,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    """Merchant marks an escalated case as lost in the Priority queue.
    Sets status=LOST, escalated_to_human=False, and writes a RecoveryActionLog
    with action_type='human_marked_lost'."""
    session, case = _resolve_priority_target(target_id, db)

    if not session and not case:
        raise HTTPException(status_code=404, detail="Session or recovery case not found")

    if case and case.merchant_id != current_merchant.id:
        raise HTTPException(status_code=403, detail="You do not have permission to modify this case.")

    reason_text = (payload.reason if payload and payload.reason else None) or "Merchant manually marked case as lost"

    if session:
        outcome = db.query(RecoveryOutcomeRecord).filter(RecoveryOutcomeRecord.session_id == session.id).first()
        if not outcome:
            outcome = RecoveryOutcomeRecord(
                session_id=session.id,
                predicted_reason="unknown",
                confidence=1.0,
                classification_method="manual",
                reasoning=reason_text,
                action_taken="manual_lost",
                action_success=False,
                delivery_status="lost"
            )
            db.add(outcome)
        else:
            outcome.action_taken = "manual_lost"
            outcome.reasoning = reason_text

    if case:
        case.status = CaseStatus.LOST
        case.escalated_to_human = False
        case.last_action_at = datetime.now(timezone.utc)
        case.next_action_due_at = None

        step = (case.ladder_step or 0) + 1
        case.ladder_step = step
        idempotency_key = f"{case.id}:{step}:human_marked_lost_{uuid.uuid4().hex[:6]}"
        action_log = RecoveryActionLog(
            case_id=case.id,
            idempotency_key=idempotency_key,
            ladder_step=step,
            action_type="human_marked_lost",
            reason=reason_text,
            guardrail_checks=json.dumps({"channel": "manual", "marked_by_merchant": current_merchant.id}),
            outcome="sent",
            amount_offered=None,
            coupon_code=None,
            requires_human_approval=False,
            approved_by=current_merchant.id,
            approved_at=datetime.now(timezone.utc),
        )
        db.add(action_log)

    db.commit()
    return {"status": "success", "message": "Case marked as lost"}


class MarkRecoveredRequest(BaseModel):
    amount_recovered: Optional[float] = None
    reason: Optional[str] = None


@app.post("/api/merchant/priority/{target_id}/recovered")
def mark_priority_recovered(
    target_id: str,
    payload: Optional[MarkRecoveredRequest] = None,
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant)
):
    """Merchant marks an escalated case as recovered manually.
    Sets status=RECOVERED, amount_recovered=payload.amount_recovered (or case.amount_at_risk),
    escalated_to_human=False, and writes a RecoveryActionLog with action_type='human_marked_recovered'."""
    session, case = _resolve_priority_target(target_id, db)

    if not session and not case:
        raise HTTPException(status_code=404, detail="Session or recovery case not found")

    if case and case.merchant_id != current_merchant.id:
        raise HTTPException(status_code=403, detail="You do not have permission to modify this case.")

    recovered_amount = (payload.amount_recovered if payload and payload.amount_recovered is not None else None)
    if recovered_amount is None:
        recovered_amount = case.amount_at_risk if case else (session.cart_value if session else 0.0)

    reason_text = (payload.reason if payload and payload.reason else None) or f"Merchant manually marked case as recovered (₹{recovered_amount:.2f})"

    if session:
        session.status = SessionStatus.RECOVERED
        session.completed_at = datetime.now(timezone.utc)
        session.final_amount_charged = recovered_amount
        outcome = db.query(RecoveryOutcomeRecord).filter(RecoveryOutcomeRecord.session_id == session.id).first()
        if not outcome:
            outcome = RecoveryOutcomeRecord(
                session_id=session.id,
                predicted_reason="unknown",
                confidence=1.0,
                classification_method="manual",
                reasoning=reason_text,
                action_taken="manual_recovered",
                action_success=True,
                delivery_status="recovered"
            )
            db.add(outcome)
        else:
            outcome.action_taken = "manual_recovered"
            outcome.reasoning = reason_text
            outcome.action_success = True

    if case:
        case.status = CaseStatus.RECOVERED
        case.amount_recovered = float(recovered_amount)
        case.escalated_to_human = False
        case.last_action_at = datetime.now(timezone.utc)
        case.next_action_due_at = None

        if case.invoice:
            case.invoice.status = InvoiceStatus.PAID
            case.invoice.paid_at = datetime.now(timezone.utc)

        step = (case.ladder_step or 0) + 1
        case.ladder_step = step
        idempotency_key = f"{case.id}:{step}:human_marked_recovered_{uuid.uuid4().hex[:6]}"
        action_log = RecoveryActionLog(
            case_id=case.id,
            idempotency_key=idempotency_key,
            ladder_step=step,
            action_type="human_marked_recovered",
            reason=reason_text,
            guardrail_checks=json.dumps({"channel": "manual", "marked_by_merchant": current_merchant.id, "amount_recovered": recovered_amount}),
            outcome="sent",
            amount_offered=None,
            coupon_code=None,
            requires_human_approval=False,
            approved_by=current_merchant.id,
            approved_at=datetime.now(timezone.utc),
        )
        db.add(action_log)

    db.commit()
    return {"status": "success", "amount_recovered": recovered_amount, "message": "Case marked as recovered"}


@app.get("/account/orders", response_class=HTMLResponse)
def customer_orders_page(request: Request):
    return templates.TemplateResponse(request, "customer_orders.html", {})

@app.get("/api/customer/orders")
def get_customer_orders(db: Session = Depends(get_db), current_customer: CustomerUser = Depends(get_current_customer)):
    sessions = db.query(CheckoutSession).filter(CheckoutSession.customer_user_id == current_customer.id).order_by(CheckoutSession.started_at.desc()).all()
    result = []
    for s in sessions:
        hydrated_items = []
        if s.cart_json:
            try:
                import json
                raw_items = json.loads(s.cart_json)
                for item in raw_items:
                    product = db.query(Product).filter(Product.id == item.get("product_id")).first()
                    hydrated_items.append({
                        "product_id": item.get("product_id"),
                        "name": product.name if product else f"Product #{item.get('product_id')}",
                        "quantity": item.get("quantity", 1),
                        "price": product.price if product else 0
                    })
            except Exception:
                pass
                
        result.append({
            "id": s.id,
            "event_id": s.event_id,
            "cart_value": s.cart_value,
            "status": s.status,
            "cart_json": json.dumps(hydrated_items) if hydrated_items else s.cart_json,
            "started_at": s.started_at.isoformat() if s.started_at else None,
        })
    return result

@app.get("/api/merchant/orders")
def get_merchant_orders(db: Session = Depends(get_db), current_merchant: MerchantUser = Depends(get_current_merchant)):
    """Returns all checkout sessions with their recovery outcomes for the
    merchant orders dashboard. Sorted newest first. Each row includes recovery audit data
    so the modal can show the full classify -> decide -> execute trail without a second request."""
    sessions = db.query(CheckoutSession).order_by(CheckoutSession.started_at.desc()).all()
    result = []
    for s in sessions:
        outcome = db.query(RecoveryOutcomeRecord).filter(RecoveryOutcomeRecord.session_id == s.id).first()
        recovery_case = db.query(RecoveryCase).filter(RecoveryCase.checkout_session_id == s.id).first()
        if not recovery_case and s.recovered_from_session_id:
            recovery_case = db.query(RecoveryCase).filter(RecoveryCase.checkout_session_id == s.recovered_from_session_id).first()

        status_str = s.status.value if hasattr(s.status, "value") else str(s.status)
        row = {
            "id": s.id,
            "event_id": s.event_id,
            "customer_name": s.customer_name,
            "customer_email": s.customer_email,
            "customer_phone": s.customer_phone,
            "cart_value": s.cart_value,
            "final_amount_charged": s.final_amount_charged or s.cart_value,
            "cart_json": s.cart_json,
            "status": status_str,
            "payment_status_code": s.payment_status_code,
            "razorpay_payment_id": s.razorpay_payment_id,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "completed_at": s.completed_at.isoformat() if s.completed_at else None,
            "abandoned_at": s.abandoned_at.isoformat() if s.abandoned_at else None,
            "recovery_case_id": recovery_case.id if recovery_case else None,
            "recovery_case_status": (recovery_case.status.value if hasattr(recovery_case.status, "value") else str(recovery_case.status)) if recovery_case else None,
            "recovery_scenario": (recovery_case.scenario.value if hasattr(recovery_case.scenario, "value") else str(recovery_case.scenario)) if recovery_case else None,
            "recovery": None,
        }
        if outcome:
            row["recovery"] = {
                "predicted_reason": outcome.predicted_reason,
                "reasoning": outcome.reasoning,
                "action_taken": outcome.action_taken,
                "action_success": outcome.action_success,
                "amount_offered": outcome.amount_offered,
                "confirmed_recovered_amount": outcome.confirmed_recovered_amount,
                "delivery_status": outcome.delivery_status,
                "payment_link_url": outcome.payment_link_url,
            }
        result.append(row)
    return result


@app.get("/merchant/login", response_class=HTMLResponse)
def merchant_login_page(request: Request):
    return templates.TemplateResponse(request, "merchant_login.html", {})


@app.get("/merchant/signup", response_class=HTMLResponse)
def merchant_signup_page(request: Request):
    return templates.TemplateResponse(request, "merchant_signup.html", {})


@app.get("/merchant", response_class=HTMLResponse)
def merchant_overview_page(request: Request, db: Session = Depends(get_db)):
    """Unlike API routes, an unauthenticated visitor here gets redirected to the
    login page instead of a raw 401 JSON response -- see get_current_merchant_or_none's
    docstring for why this needs a separate function from the API's auth dependency."""
    merchant = get_current_merchant_or_none(request, db)
    if merchant is None:
        return RedirectResponse(url="/merchant/login")

    return templates.TemplateResponse(
        request, "merchant_overview.html",
        {"store_name": merchant.store_name, "active_section": "overview"},
    )


@app.get("/merchant/products", response_class=HTMLResponse)
def merchant_products_page(request: Request, db: Session = Depends(get_db)):
    merchant = get_current_merchant_or_none(request, db)
    if merchant is None:
        return RedirectResponse(url="/merchant/login")

    return templates.TemplateResponse(
        request, "merchant_products.html",
        {"store_name": merchant.store_name, "active_section": "products"},
    )


@app.get("/merchant/orders", response_class=HTMLResponse)
def merchant_orders_page(request: Request, db: Session = Depends(get_db)):
    """Placeholder for now -- full orders list with filtering is the next build step."""
    merchant = get_current_merchant_or_none(request, db)
    if merchant is None:
        return RedirectResponse(url="/merchant/login")

    return templates.TemplateResponse(
        request, "merchant_orders.html",
        {"store_name": merchant.store_name, "active_section": "orders"},
    )


@app.get("/merchant/checkout-abandonment", response_class=HTMLResponse)
def merchant_checkout_abandonment_page(request: Request, db: Session = Depends(get_db)):
    """Checkout abandonment recovery dashboard tab."""
    merchant = get_current_merchant_or_none(request, db)
    if merchant is None:
        return RedirectResponse(url="/merchant/login")

    return templates.TemplateResponse(
        request, "merchant_checkout_abandonment.html",
        {"store_name": merchant.store_name, "active_section": "checkout-abandonment"},
    )


@app.get("/merchant/payment-failures", response_class=HTMLResponse)
def merchant_payment_failures_page(request: Request, db: Session = Depends(get_db)):
    """Payment failure recovery dashboard tab."""
    merchant = get_current_merchant_or_none(request, db)
    if merchant is None:
        return RedirectResponse(url="/merchant/login")

    return templates.TemplateResponse(
        request, "merchant_payment_failures.html",
        {"store_name": merchant.store_name, "active_section": "payment-failures"},
    )


@app.get("/merchant/receivables", response_class=HTMLResponse)
def merchant_receivables_page(request: Request, db: Session = Depends(get_db)):
    """Overdue receivables recovery dashboard tab."""
    merchant = get_current_merchant_or_none(request, db)
    if merchant is None:
        return RedirectResponse(url="/merchant/login")

    return templates.TemplateResponse(
        request, "merchant_receivables.html",
        {"store_name": merchant.store_name, "active_section": "receivables"},
    )



def get_llm_client():
    """FastAPI dependency for the classifier's LLM fallback. Same client used by
    the batch pipeline -- if ANTHROPIC_API_KEY has no credit, classify() already
    degrades gracefully to 'unknown' + flag_for_manual_review (proven in the
    batch runs, see CHALLENGES.md), so this is safe to call even with no credits."""
    try:
        import anthropic
        return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    except Exception:
        return None


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


@app.get("/api/merchant/analytics")
def get_merchant_analytics(
    db: Session = Depends(get_db),
    current_merchant: MerchantUser = Depends(get_current_merchant),
):
    """Returns recovery analytics for the merchant's store."""
    return compute_live_analytics(db, merchant_id=current_merchant.id)


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
    resume_event_id: Optional[str] = None
    coupon_code: Optional[str] = None
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
    payment_status_code: Optional[str] = None  # set when Razorpay fires payment.failed
                                                # (e.g. "insufficient_funds", "gateway_timeout").
                                                # When present, the backend routes to the
                                                # payment-failure pipeline instead of the
                                                # plain abandonment pipeline.
    error_source: Optional[str] = None  # Razorpay's real error.source from payment.failed
                                        # ("bank" | "customer" | "gateway" | "business").
                                        # When present, takes precedence over the rule table's
                                        # inferred source — Razorpay's source always wins.


class CouponValidateRequest(BaseModel):
    code: str
    cart_total: float

@app.post("/api/coupons/validate")
def validate_coupon(payload: CouponValidateRequest, db: Session = Depends(get_db)):
    # Assuming code is unique globally or we need merchant_id? 
    # For this demo storefront, it's a single store. So we just search by code.
    coupon = db.query(Coupon).filter(Coupon.code == payload.code.upper()).first()
    
    if not coupon:
        raise HTTPException(status_code=400, detail="Invalid coupon code")
        
    if not coupon.active:
        raise HTTPException(status_code=400, detail="Coupon is no longer active")
        
    if coupon.usage_limit is not None and coupon.times_used >= coupon.usage_limit:
        raise HTTPException(status_code=400, detail="Coupon usage limit reached")
        
    discount_amount = 0.0
    if coupon.discount_pct:
        discount_amount = payload.cart_total * (coupon.discount_pct / 100.0)
    elif coupon.discount_amount:
        discount_amount = coupon.discount_amount
        
    new_total = max(0.0, payload.cart_total - discount_amount)
    
    return {
        "status": "success",
        "coupon_id": coupon.id,
        "code": coupon.code,
        "discount_amount": discount_amount,
        "new_total": new_total
    }

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

    amount_to_charge = cart_value
    recovered_from_session_id = None
    if payload.resume_event_id:
        prev_session = db.query(CheckoutSession).filter(CheckoutSession.event_id == payload.resume_event_id, CheckoutSession.customer_user_id == current_customer.id).first()
        if prev_session:
            recovered_from_session_id = prev_session.id
            
            # Prevent duplicate orders: if they click the resume link multiple times, reuse the active resumed session
            # ONLY if no new coupon is being applied (to keep it simple). 
            existing_resume = db.query(CheckoutSession).filter(
                CheckoutSession.recovered_from_session_id == prev_session.id,
                CheckoutSession.status == SessionStatus.STARTED
            ).first()
            if existing_resume and not payload.coupon_code:
                # Need to return the same order ID, but wait, we didn't save Razorpay order_id in CheckoutSession!
                # If we didn't save it, we HAVE to create a new order. But creating a new order means creating a new row?
                # Actually, let's just use the existing logic but avoid duplicate coupon increments.
                pass
                
            outcome = db.query(RecoveryOutcomeRecord).filter(RecoveryOutcomeRecord.session_id == prev_session.id).first()
            if outcome and outcome.amount_offered is not None and prev_session.cart_value and prev_session.cart_value > 0:
                discount_ratio = max(0.0, (prev_session.cart_value - outcome.amount_offered) / prev_session.cart_value)
                if discount_ratio > 0:
                    amount_to_charge = max(1.0, round(cart_value * (1.0 - discount_ratio), 2))
                else:
                    amount_to_charge = cart_value
                
    if not recovered_from_session_id:
        # Fallback attribution: if they didn't explicitly use the resume link but they ARE completing
        # an order shortly after abandoning one (e.g. they switched tabs and typed the emailed coupon manually),
        # attribute it to their most recent abandoned session.
        recent_abandoned = db.query(CheckoutSession).filter(
            CheckoutSession.customer_user_id == current_customer.id,
            CheckoutSession.status == SessionStatus.ABANDONED
        ).order_by(CheckoutSession.abandoned_at.desc()).first()
        if recent_abandoned:
            outcome = db.query(RecoveryOutcomeRecord).filter(RecoveryOutcomeRecord.session_id == recent_abandoned.id).first()
            if outcome:
                recovered_from_session_id = recent_abandoned.id

    # Fetch merchant to check high value threshold
    merchant = None
    if payload.cart_items:
        product = db.query(Product).filter(Product.id == payload.cart_items[0].product_id).first()
        if product:
            merchant = db.query(MerchantUser).filter(MerchantUser.id == product.merchant_id).first()
            
    # Apply custom coupon if provided and valid
    applied_coupon_id = None
    if payload.coupon_code and merchant:
        coupon = db.query(Coupon).filter(
            Coupon.code == payload.coupon_code.strip().upper(), 
            Coupon.merchant_id == merchant.id,
            Coupon.active == True
        ).first()
        if coupon:
            # GUARDRAIL: If this is a resumed checkout from a technical failure, they are not allowed ANY discount.
            discount_allowed = True
            if payload.resume_event_id:
                prev_session = db.query(CheckoutSession).filter(CheckoutSession.event_id == payload.resume_event_id).first()
                if prev_session:
                    outcome = db.query(RecoveryOutcomeRecord).filter(RecoveryOutcomeRecord.session_id == prev_session.id).first()
                    if outcome:
                        from app.agent.recovery_actions import is_discount_allowed
                        if not is_discount_allowed(outcome.predicted_reason):
                            discount_allowed = False
            
            if discount_allowed:
                if coupon.usage_limit is None or coupon.times_used < coupon.usage_limit:
                    # Apply coupon (overriding automatic outcome amount if any)
                    if coupon.discount_pct:
                        amount_to_charge = cart_value * (1 - (coupon.discount_pct / 100.0))
                    elif coupon.discount_amount:
                        amount_to_charge = max(1.0, cart_value - coupon.discount_amount)
                        
                    amount_to_charge = max(1.0, amount_to_charge) # Razorpay minimum is 1 INR
                else:
                    raise HTTPException(status_code=400, detail="Coupon usage limit reached")
                applied_coupon_id = coupon.id
            else:
                raise HTTPException(status_code=400, detail="Coupons cannot be applied to this session")
        else:
            raise HTTPException(status_code=400, detail="Invalid or inactive coupon code")

    event_id = f"chk_{uuid.uuid4().hex[:12]}"

    order_result = razorpay_client.create_order(amount_rupees=amount_to_charge, receipt=event_id)
    if not order_result.success:
        raise HTTPException(status_code=502, detail=f"Could not create payment order: {order_result.error_message}")

    is_high_priority = cart_value >= (merchant.high_value_threshold_amount if merchant else 3000.0)

    session = CheckoutSession(
        event_id=event_id,
        customer_user_id=current_customer.id,
        customer_name=current_customer.name,
        customer_email=current_customer.email,
        customer_phone=current_customer.phone or "",
        cart_value=cart_value,
        final_amount_charged=amount_to_charge,
        cart_json=json.dumps([item.model_dump() for item in payload.cart_items]),
        status=SessionStatus.STARTED,
        is_high_priority=is_high_priority,
        recovered_from_session_id=recovered_from_session_id,
        applied_coupon_id=applied_coupon_id
    )
    db.add(session)
    db.commit()

    return CheckoutStartResponse(
        event_id=event_id,
        order_id=order_result.order_id,
        amount_paise=int(round(amount_to_charge * 100)),
        razorpay_key_id=razorpay_client.key_id,
        cart_value=amount_to_charge,
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

    # ── Close out the matching RecoveryCase (new table) ───────────────────────
    # Two lookup paths must be tried:
    #   1. checkout_session_id == session.id
    #      → payment-failure recovery: the customer retried on the SAME session
    #        after receiving a recovery payment link.
    #   2. checkout_session_id == session.recovered_from_session_id
    #      → abandonment recovery: the customer started a NEW session from a
    #        recovery link pointing back to the original abandoned session.
    now = datetime.now(timezone.utc)
    recovered_amount = session.final_amount_charged or session.cart_value

    # Look up coupon applied to this session (if any)
    coupon_code_used = None
    discount_amount_val = 0.0
    if session.applied_coupon_id:
        c_obj = db.query(Coupon).filter(Coupon.id == session.applied_coupon_id).first()
        if c_obj:
            coupon_code_used = c_obj.code
            discount_amount_val = max(0.0, session.cart_value - (session.final_amount_charged or session.cart_value))

    recovery_case = db.query(RecoveryCase).filter(
        RecoveryCase.checkout_session_id == session.id
    ).first()

    if recovery_case is None and session.recovered_from_session_id:
        recovery_case = db.query(RecoveryCase).filter(
            RecoveryCase.checkout_session_id == session.recovered_from_session_id
        ).first()

    if recovery_case is not None and recovery_case.status != CaseStatus.RECOVERED:
        recovery_case.status = CaseStatus.RECOVERED
        recovery_case.amount_recovered = recovered_amount
        recovery_case.coupon_code_used = coupon_code_used
        recovery_case.discount_amount = discount_amount_val
        recovery_case.last_action_at = now
        recovery_case.next_action_due_at = None   # cancel any pending scheduled ladder steps

        # Write an idempotent audit row so the merchant can see the closure event
        # in the audit trail.  idempotency_key prevents double-fire if
        # complete_checkout is somehow called twice for the same session.
        idempotency_key = f"{recovery_case.id}:recovered:{session.event_id}"
        audit_log = RecoveryActionLog(
            case_id=recovery_case.id,
            idempotency_key=idempotency_key,
            ladder_step=recovery_case.ladder_step,
            action_type="case_recovered",
            reason="Customer completed payment — case closed as RECOVERED." + (f" Used coupon: {coupon_code_used}" if coupon_code_used else ""),
            guardrail_checks=json.dumps({
                "confirmed_payment_id": payload.razorpay_payment_id,
                "session_event_id": session.event_id,
                "amount_recovered": recovered_amount,
                "coupon_code_used": coupon_code_used,
                "discount_amount": discount_amount_val,
            }),
            outcome="sent",
            amount_offered=recovered_amount if discount_amount_val > 0 else None,
            coupon_code=coupon_code_used,
            requires_human_approval=False,
        )
        db.add(audit_log)
        try:
            db.flush()  # catch duplicate idempotency_key early before commit
        except Exception:
            db.rollback()
            # Already written (duplicate call) — safe to proceed; case was closed
            # on the first call, so no further action is needed.
            logging.getLogger("recovery_agent.complete_checkout").warning(
                "Duplicate complete_checkout for event_id=%s — audit log already exists, skipping.",
                session.event_id,
            )
            # Re-fetch the session to avoid working with a rolled-back state
            session = db.query(CheckoutSession).filter(
                CheckoutSession.event_id == payload.event_id
            ).first()

    # ── Update the RecoveryOutcomeRecord (OLD table — unchanged) ─────────────
    if session.recovered_from_session_id:
        outcome = db.query(RecoveryOutcomeRecord).filter(
            RecoveryOutcomeRecord.session_id == session.recovered_from_session_id
        ).first()
        if outcome:
            outcome.confirmed_recovered_amount = session.final_amount_charged or session.cart_value

    if session.applied_coupon_id:
        coupon = db.query(Coupon).filter(Coupon.id == session.applied_coupon_id).first()
        if coupon:
            coupon.times_used += 1

            # Log the coupon usage
            discount_amount = max(0.0, session.cart_value - (session.final_amount_charged or session.cart_value))
            usage_log = CouponUsageLog(
                coupon_id=coupon.id,
                customer_email=current_customer.email,
                order_value=session.final_amount_charged or session.cart_value,
                discount_amount=discount_amount,
            )
            db.add(usage_log)

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
    """Called by the frontend when the customer closes the Razorpay popup
    (Checkout.js 'ondismiss' callback) OR when Razorpay fires 'payment.failed'.

    Routing logic (new — does NOT change existing abandonment behaviour):
      - If payload.payment_status_code is present → PAYMENT_FAILURE pipeline:
          classify_payment_failure() + run_payment_failure_recovery()
          Creates/updates RecoveryCase + RecoveryActionLog. Returns immediately;
          the existing RecoveryOutcomeOut is returned as None since the
          abandonment outcome record is not written for payment failures.
      - If payload.payment_status_code is absent → original ABANDONMENT pipeline:
          run_recovery_for_session() exactly as before, no changes.

    Race-condition guard: if the payment succeeded milliseconds before dismiss
    fires, we never downgrade a COMPLETED session."""
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

    # If a payment_status_code was reported, stamp it on the session so the DB
    # row is queryable later without touching the existing RecoveryOutcomeRecord schema.
    if payload.payment_status_code:
        session.payment_status_code = payload.payment_status_code

    db.commit()

    # ── Route: payment failure vs plain abandonment ───────────────────────────
    if payload.payment_status_code:
        # PAYMENT_FAILURE path — new pipeline, no RecoveryOutcomeRecord written
        from app.agent.payment_failure_classifier import classify_payment_failure
        from app.agent.payment_failure_actions import run_payment_failure_recovery

        import logging as _logging
        _pf_log = _logging.getLogger("recovery_agent.abandon_checkout")
        _pf_log.info(
            "[abandon_checkout] PAYMENT_FAILURE signal received — event_id=%s "
            "payment_status_code=%r  error_source=%r",
            payload.event_id, payload.payment_status_code, payload.error_source,
        )

        classification = classify_payment_failure(
            session,
            llm_client=None,
            error_source=payload.error_source,
        )
        _pf_log.info(
            "[abandon_checkout] Classification result — method=%s  source=%s  "
            "failure_class=%s  confidence=%.2f",
            classification.method, classification.source,
            classification.failure_class, classification.confidence,
        )
        run_payment_failure_recovery(
            session=session,
            classification=classification,
            db=db,
            razorpay_client=recovery_razorpay_client,
        )
        # Return a status the frontend can distinguish from plain abandonment.
        # recovery=None is intentional: the action lives in RecoveryCase/RecoveryActionLog,
        # not RecoveryOutcomeRecord, and we don't want to conflate the two schemas.
        return AbandonResponse(
            status="payment_failure_recovery_initiated",
            event_id=session.event_id,
            recovery=None,
        )

    # ── Original ABANDONMENT path — unchanged byte for byte ───────────────────
    outcome_record = run_recovery_for_session(
        session=session, db=db, razorpay_client=recovery_razorpay_client, llm_client=llm_client
    )

    return AbandonResponse(
        status="abandoned",
        event_id=session.event_id,
        recovery=RecoveryOutcomeOut.model_validate(outcome_record),
    )



class MerchantProfileUpdate(BaseModel):
    store_name: Optional[str]
    contact_email: Optional[str]
    phone: Optional[str]
    logo_url: Optional[str]

@app.get("/merchant/profile", response_class=HTMLResponse)
def merchant_profile_page(request: Request):
    return templates.TemplateResponse(request, "merchant_profile.html", {"active_section": "profile"})

@app.get("/api/merchant/profile")
def get_merchant_profile(db: Session = Depends(get_db), current_merchant: MerchantUser = Depends(get_current_merchant)):
    return {
        "store_name": current_merchant.store_name,
        "contact_email": current_merchant.contact_email,
        "phone": current_merchant.phone,
        "logo_url": current_merchant.logo_url
    }

@app.post("/api/merchant/profile")
def update_merchant_profile(payload: MerchantProfileUpdate, db: Session = Depends(get_db), current_merchant: MerchantUser = Depends(get_current_merchant)):
    current_merchant.store_name = payload.store_name
    current_merchant.contact_email = payload.contact_email
    current_merchant.phone = payload.phone
    current_merchant.logo_url = payload.logo_url
    db.commit()
    return {"status": "success"}

import re
from pydantic import field_validator

class CustomerProfileUpdate(BaseModel):
    name: Optional[str]
    email: Optional[str]
    phone: Optional[str]
    opted_out_of_marketing: bool

    @field_validator('phone', mode='after')
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return v
        v = re.sub(r'[\s\-\(\)]', '', v)
        if not v.startswith('+'):
            v = '+91' + v
        if not re.match(r'^\+91\d{10}$', v):
            raise ValueError('Phone number must be exactly 10 digits')
        return v

@app.get("/account/profile", response_class=HTMLResponse)
def customer_profile_page(request: Request):
    return templates.TemplateResponse(request, "customer_profile.html", {})

@app.get("/api/customer/profile")
def get_customer_profile(db: Session = Depends(get_db), current_customer: CustomerUser = Depends(get_current_customer)):
    return {
        "id": current_customer.id,
        "name": current_customer.name,
        "email": current_customer.email,
        "phone": current_customer.phone,
        "opted_out_of_marketing": current_customer.opted_out_of_marketing
    }

@app.post("/api/customer/profile")
def update_customer_profile(payload: CustomerProfileUpdate, db: Session = Depends(get_db), current_customer: CustomerUser = Depends(get_current_customer)):
    current_customer.name = payload.name
    current_customer.email = payload.email
    current_customer.phone = payload.phone
    current_customer.opted_out_of_marketing = payload.opted_out_of_marketing
    db.commit()
    return {"status": "success"}
