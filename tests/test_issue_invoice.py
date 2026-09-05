"""
Tests for POST /api/merchant/invoices and Overdue Receivable RecoveryCase creation.

Verifies:
1. Happy path invoice creation with demo shortcut due date (e.g. '5m') creates both Invoice and linked RecoveryCase.
2. Custom invoice number and ISO due date support.
3. Merchant isolation: invoices and recovery cases are strictly scoped to the authenticated merchant.
4. Validation guards: invalid customer, negative/zero amount, duplicate invoice number, invalid date.
"""

import os
from datetime import datetime, timezone, timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db_models import (
    Base, MerchantUser, CustomerUser,
    Invoice, InvoiceStatus,
    RecoveryCase, RecoveryScenario, CaseStatus,
)
from app.db import get_db
from app.main import app
from app.merchant_auth_routes import get_current_merchant


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(engine)


@pytest.fixture()
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def seed_data(db_session):
    # Merchant 1
    m1 = MerchantUser(
        email="merchant1@example.com",
        password_hash="hash1",
        store_name="Store 1",
    )
    # Merchant 2
    m2 = MerchantUser(
        email="merchant2@example.com",
        password_hash="hash2",
        store_name="Store 2",
    )
    # Customer
    c1 = CustomerUser(
        email="customer1@example.com",
        password_hash="hashcust",
        name="Alice Receivables",
        phone="+919876543210",
    )
    db_session.add_all([m1, m2, c1])
    db_session.commit()
    db_session.refresh(m1)
    db_session.refresh(m2)
    db_session.refresh(c1)
    return {"m1": m1, "m2": m2, "c1": c1}


def test_issue_invoice_happy_path_demo_shortcut(client, db_session, seed_data):
    """Confirm issuing an invoice creates an Invoice row and linked RecoveryCase."""
    m1 = seed_data["m1"]
    c1 = seed_data["c1"]

    app.dependency_overrides[get_current_merchant] = lambda: m1

    payload = {
        "customer_id": c1.id,
        "amount": 7500.0,
        "due_date": "5m",  # demo mode: due in 5 minutes
    }

    res = client.post("/api/merchant/invoices", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["status"] == "success"
    assert "invoice" in data
    assert "recovery_case" in data

    inv_data = data["invoice"]
    case_data = data["recovery_case"]

    # Assert Invoice persisted in DB
    inv = db_session.query(Invoice).filter(Invoice.id == inv_data["id"]).first()
    assert inv is not None
    assert inv.merchant_id == m1.id
    assert inv.customer_user_id == c1.id
    assert inv.amount == 7500.0
    assert inv.currency == "INR"
    assert inv.status == InvoiceStatus.PENDING
    assert inv.invoice_number.startswith("INV-")

    # Due date should be roughly 5 minutes in future
    now = datetime.now(timezone.utc)
    due_tz = inv.due_date.replace(tzinfo=timezone.utc) if inv.due_date.tzinfo is None else inv.due_date
    diff = (due_tz - now).total_seconds()
    assert 200 < diff < 400  # around 300 seconds (5 mins)

    # Assert RecoveryCase created and linked
    case = db_session.query(RecoveryCase).filter(RecoveryCase.id == case_data["id"]).first()
    assert case is not None
    assert case.merchant_id == m1.id
    assert case.customer_user_id == c1.id
    assert case.invoice_id == inv.id
    assert case.scenario == RecoveryScenario.OVERDUE_RECEIVABLE
    assert case.status == CaseStatus.NEW
    assert case.amount_at_risk == 7500.0
    assert case.amount_recovered == 0.0
    assert case.ladder_step == 0


def test_issue_invoice_custom_number_and_iso_due_date(client, db_session, seed_data):
    """Confirm custom invoice number and explicit ISO due date are respected."""
    m1 = seed_data["m1"]
    c1 = seed_data["c1"]

    app.dependency_overrides[get_current_merchant] = lambda: m1

    custom_inv_num = "INV-TEST-2026-999"
    due_iso = "2026-10-01T15:30:00"

    payload = {
        "customer_id": c1.id,
        "amount": 12000.0,
        "invoice_number": custom_inv_num,
        "due_date": due_iso,
    }

    res = client.post("/api/merchant/invoices", json=payload)
    assert res.status_code == 200, res.text
    data = res.json()

    assert data["invoice"]["invoice_number"] == custom_inv_num
    assert data["invoice"]["amount"] == 12000.0

    inv = db_session.query(Invoice).filter(Invoice.invoice_number == custom_inv_num).first()
    assert inv is not None
    assert inv.amount == 12000.0

    case = db_session.query(RecoveryCase).filter(RecoveryCase.invoice_id == inv.id).first()
    assert case is not None
    assert case.amount_at_risk == 12000.0
    assert case.scenario == RecoveryScenario.OVERDUE_RECEIVABLE


def test_merchant_isolation_on_invoices(client, db_session, seed_data):
    """Confirm merchant B cannot see invoices issued by merchant A."""
    m1 = seed_data["m1"]
    m2 = seed_data["m2"]
    c1 = seed_data["c1"]

    # Merchant 1 creates an invoice
    app.dependency_overrides[get_current_merchant] = lambda: m1
    res1 = client.post("/api/merchant/invoices", json={
        "customer_id": c1.id,
        "amount": 5000.0,
        "due_date": "7d",
    })
    assert res1.status_code == 200

    # Merchant 1 lists invoices -> has 1
    res_list1 = client.get("/api/merchant/invoices")
    assert res_list1.status_code == 200
    assert len(res_list1.json()) == 1

    # Switch to Merchant 2 -> lists invoices -> has 0
    app.dependency_overrides[get_current_merchant] = lambda: m2
    res_list2 = client.get("/api/merchant/invoices")
    assert res_list2.status_code == 200
    assert len(res_list2.json()) == 0


def test_issue_invoice_validation_guards(client, db_session, seed_data):
    """Test validation errors for invalid customer, negative amount, duplicate number, bad date."""
    m1 = seed_data["m1"]
    c1 = seed_data["c1"]
    app.dependency_overrides[get_current_merchant] = lambda: m1

    # 1. Invalid customer ID
    res = client.post("/api/merchant/invoices", json={"customer_id": 9999, "amount": 1000.0})
    assert res.status_code == 404
    assert "Customer ID 9999 not found" in res.json()["detail"]

    # 2. Non-positive amount
    res = client.post("/api/merchant/invoices", json={"customer_id": c1.id, "amount": 0.0})
    assert res.status_code == 400
    assert "greater than 0" in res.json()["detail"]

    # 3. Duplicate invoice number
    res = client.post("/api/merchant/invoices", json={
        "customer_id": c1.id,
        "amount": 1000.0,
        "invoice_number": "INV-DUP-1",
    })
    assert res.status_code == 200

    res_dup = client.post("/api/merchant/invoices", json={
        "customer_id": c1.id,
        "amount": 2000.0,
        "invoice_number": "INV-DUP-1",
    })
    assert res_dup.status_code == 400
    assert "already exists" in res_dup.json()["detail"]

    # 4. Invalid due date format
    res_bad_date = client.post("/api/merchant/invoices", json={
        "customer_id": c1.id,
        "amount": 1000.0,
        "due_date": "not-a-date",
    })
    assert res_bad_date.status_code == 400
    assert "Invalid due_date format" in res_bad_date.json()["detail"]
