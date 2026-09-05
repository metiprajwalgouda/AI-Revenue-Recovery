"""
Test Suite: Customer Listing, Customer Details Modal Endpoint, and Broadcast Messaging
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from app.main import app
from app.db import get_db, Base, engine, SessionLocal
from app.db_models import MerchantUser, CustomerUser, CheckoutSession, SessionStatus, RecoveryCase, RecoveryScenario, CaseStatus, Coupon
from app.auth import create_merchant_session_token, MERCHANT_COOKIE_NAME

client = TestClient(app)

PASS = "✅ PASS"
FAIL = "❌ FAIL"

def check(name: str, cond: bool, val=None):
    if cond:
        print(f"  {PASS}  {name}")
    else:
        print(f"  {FAIL}  {name} (got {val!r})")
        raise AssertionError(f"{name} failed with {val!r}")

def setup_test_data():
    db = SessionLocal()
    try:
        # Create merchant
        m = db.query(MerchantUser).filter(MerchantUser.id == 1).first()
        if not m:
            m = MerchantUser(id=1, email="merchant1@example.com", store_name="Demo Store", password_hash="fakehash")
            db.add(m)
            db.commit()

        # Create customers
        c1 = db.query(CustomerUser).filter(CustomerUser.email == "alice_cust@example.com").first()
        if not c1:
            c1 = CustomerUser(email="alice_cust@example.com", name="Alice Wonderland", phone="+919876543210", password_hash="fakehash", opted_out_of_marketing=False)
            db.add(c1)
            db.commit()
            db.refresh(c1)

        c2 = db.query(CustomerUser).filter(CustomerUser.email == "bob_optout@example.com").first()
        if not c2:
            c2 = CustomerUser(email="bob_optout@example.com", name="Bob Optout", phone="+919876543211", password_hash="fakehash", opted_out_of_marketing=True)
            db.add(c2)
            db.commit()
            db.refresh(c2)

        # Create session for Alice
        s1 = db.query(CheckoutSession).filter(CheckoutSession.event_id == "alice_sess_1").first()
        if not s1:
            s1 = CheckoutSession(
                event_id="alice_sess_1",
                customer_user_id=c1.id,
                customer_email=c1.email,
                customer_name=c1.name,
                customer_phone=c1.phone,
                cart_value=2500.0,
                status=SessionStatus.COMPLETED,
                cart_json=json.dumps([{"product_id": 1, "name": "Wireless Headphones", "quantity": 1, "price": 2500.0}])
            )
            db.add(s1)
            db.commit()

        # Create coupon
        coup = db.query(Coupon).filter(Coupon.code == "BROADCAST10", Coupon.merchant_id == 1).first()
        if not coup:
            coup = Coupon(merchant_id=1, code="BROADCAST10", discount_pct=10, active=True)
            db.add(coup)
            db.commit()
            db.refresh(coup)

        return m.id, c1.id, c2.id, coup.id
    finally:
        db.close()

def run_tests():
    print("=" * 70)
    print("Customer Details & Broadcast Messaging Test Suite")
    print("=" * 70)

    m_id, c1_id, c2_id, coup_id = setup_test_data()
    token = create_merchant_session_token(m_id)
    headers = {"Cookie": f"{MERCHANT_COOKIE_NAME}={token}"}

    # 1. Test GET /api/merchant/customers
    print("\n[1] GET /api/merchant/customers")
    res = client.get("/api/merchant/customers", headers=headers)
    check("status 200", res.status_code == 200, res.status_code)
    custs = res.json()
    check("customers returned as list", isinstance(custs, list))
    check("at least 2 customers present", len(custs) >= 2)
    alice = next((c for c in custs if c["id"] == c1_id), None)
    check("alice present", alice is not None)
    check("alice total_orders >= 1", alice["total_orders"] >= 1, alice.get("total_orders"))
    check("alice total_spend >= 2500", alice["total_spend"] >= 2500.0, alice.get("total_spend"))

    # 2. Test GET /api/merchant/customers/{customer_id}
    print("\n[2] GET /api/merchant/customers/{customer_id}")
    res_detail = client.get(f"/api/merchant/customers/{c1_id}", headers=headers)
    check("status 200", res_detail.status_code == 200, res_detail.status_code)
    detail = res_detail.json()
    check("has customer field", "customer" in detail)
    check("customer name matches", detail["customer"]["name"] == "Alice Wonderland", detail["customer"].get("name"))
    check("customer email matches", detail["customer"]["email"] == "alice_cust@example.com")
    check("has sessions list", isinstance(detail.get("sessions"), list))
    check("sessions count >= 1", len(detail["sessions"]) >= 1)
    check("session items hydrated", len(detail["sessions"][0]["items"]) >= 1)
    check("item name present", bool(detail["sessions"][0]["items"][0]["name"]), detail["sessions"][0]["items"][0]["name"])
    check("has recovery_cases list", isinstance(detail.get("recovery_cases"), list))
    check("has invoices list", isinstance(detail.get("invoices"), list))

    # 3. Test non-existent customer 404
    print("\n[3] GET /api/merchant/customers/999999 (Non-existent)")
    res_404 = client.get("/api/merchant/customers/999999", headers=headers)
    check("status 404", res_404.status_code == 404, res_404.status_code)

    # 4. Test POST /api/merchant/broadcast
    print("\n[4] POST /api/merchant/broadcast")
    from unittest.mock import patch
    broadcast_payload = {
        "customer_ids": [c1_id, c2_id],
        "coupon_id": coup_id,
        "message": "Special flash sale for our VIP customers!"
    }
    with patch("app.merchant_extensions.send_recovery_email", return_value=True):
        res_bc = client.post("/api/merchant/broadcast", json=broadcast_payload, headers=headers)
    check("status 200", res_bc.status_code == 200, res_bc.status_code)
    bc_data = res_bc.json()
    check("status is success", bc_data.get("status") == "success")
    check("sent == 1 (Alice sent)", bc_data.get("sent") == 1, bc_data.get("sent"))
    check("skipped == 1 (Bob opted out)", bc_data.get("skipped") == 1, bc_data.get("skipped"))

    # 5. Test Customers Page HTML renders
    print("\n[5] GET /merchant/customers HTML page")
    res_page = client.get("/merchant/customers", headers=headers)
    check("status 200", res_page.status_code == 200, res_page.status_code)
    check("contains customer details modal", "customer-detail-modal" in res_page.text)
    check("contains Send Broadcast Offer button", ("Send Promotional Offer" in res_page.text or "Send Promotional Broadcast Offer" in res_page.text or "Send Broadcast" in res_page.text))
    # Confirm top header nav does not contain the old duplicate logout link
    check("top header logout removed from base_merchant", "merchant-logout-link" not in res_page.text)

    print("\n" + "=" * 70)
    print("RESULT: ALL CUSTOMER & BROADCAST TESTS PASSED " + PASS)
    print("=" * 70)

if __name__ == "__main__":
    run_tests()
