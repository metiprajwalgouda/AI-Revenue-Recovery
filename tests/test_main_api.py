"""
Integration tests for the product management API.

Uses a SEPARATE test database (not data/storefront.db) so running tests never
touches real merchant data, and each test run starts from a clean slate.
"""

import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("ANTHROPIC_API_KEY", "test_placeholder")
os.environ.setdefault("RAZORPAY_KEY_ID", "test_key")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_secret")

from app.db_models import Base
from app import db as db_module
from app import main


@pytest.fixture(autouse=True)
def isolated_test_db(tmp_path, monkeypatch):
    """Points the app at a throwaway SQLite file for the duration of each test,
    so tests never touch data/storefront.db and never leak state between tests."""
    test_db_path = tmp_path / "test_storefront.db"
    test_engine = create_engine(f"sqlite:///{test_db_path}", connect_args={"check_same_thread": False})
    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    Base.metadata.create_all(bind=test_engine)

    def override_get_db():
        session = TestSessionLocal()
        try:
            yield session
        finally:
            session.close()

    main.app.dependency_overrides[db_module.get_db] = override_get_db
    yield
    main.app.dependency_overrides.clear()


@pytest.fixture
def client():
    """Returns a TestClient that's already signed up + logged in as a merchant --
    product-management endpoints now require merchant auth (see merchant_auth_routes.py),
    and TestClient persists cookies across requests within the same instance, so every
    test using this fixture is authenticated as the same merchant automatically."""
    c = TestClient(main.app)
    signup = c.post("/api/merchant/signup", json={
        "email": "merchant@teststore.com", "password": "testpassword123", "store_name": "Test Store",
    })
    assert signup.status_code == 200, f"Merchant signup fixture failed: {signup.text}"
    return c


def test_create_and_get_product(client):
    response = client.post("/api/products", json={
        "name": "Wireless Mouse", "description": "A mouse", "price": 899.0, "stock": 25,
    })
    assert response.status_code == 200
    product = response.json()
    assert product["name"] == "Wireless Mouse"
    assert product["is_active"] is True

    get_response = client.get(f"/api/products/{product['id']}")
    assert get_response.status_code == 200
    assert get_response.json()["price"] == 899.0


def test_create_product_rejects_zero_price(client):
    response = client.post("/api/products", json={
        "name": "Free Item", "price": 0, "stock": 10,
    })
    assert response.status_code == 422  # pydantic validation: price must be > 0


def test_create_product_rejects_negative_stock(client):
    response = client.post("/api/products", json={
        "name": "Bad Stock Item", "price": 100, "stock": -5,
    })
    assert response.status_code == 422


def test_list_products_excludes_inactive_by_default(client):
    active = client.post("/api/products", json={"name": "Active Item", "price": 100, "stock": 5}).json()
    inactive_id = client.post("/api/products", json={"name": "To Delete", "price": 200, "stock": 3}).json()["id"]
    client.delete(f"/api/products/{inactive_id}")

    response = client.get("/api/products")
    names = [p["name"] for p in response.json()]
    assert "Active Item" in names
    assert "To Delete" not in names


def test_list_products_includes_inactive_when_requested(client):
    inactive_id = client.post("/api/products", json={"name": "Hidden Item", "price": 150, "stock": 2}).json()["id"]
    client.delete(f"/api/products/{inactive_id}")

    response = client.get("/api/products?include_inactive=true")
    names = [p["name"] for p in response.json()]
    assert "Hidden Item" in names


def test_get_nonexistent_product_returns_404(client):
    response = client.get("/api/products/99999")
    assert response.status_code == 404


def test_update_nonexistent_product_returns_404(client):
    response = client.patch("/api/products/99999", json={"price": 500})
    assert response.status_code == 404


def test_partial_update_only_changes_specified_fields(client):
    created = client.post("/api/products", json={
        "name": "Original Name", "description": "Original desc", "price": 500.0, "stock": 10,
    }).json()

    update_response = client.patch(f"/api/products/{created['id']}", json={"price": 750.0})
    assert update_response.status_code == 200
    updated = update_response.json()
    assert updated["price"] == 750.0
    assert updated["name"] == "Original Name"  # unchanged
    assert updated["description"] == "Original desc"  # unchanged


def test_soft_delete_twice_does_not_error(client):
    created = client.post("/api/products", json={"name": "Delete Me", "price": 300, "stock": 1}).json()
    first_delete = client.delete(f"/api/products/{created['id']}")
    second_delete = client.delete(f"/api/products/{created['id']}")
    assert first_delete.status_code == 200
    assert second_delete.status_code == 200  # deactivating an already-inactive product is fine, not an error


def test_health_check(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------- Storefront frontend routes (Day 2) ----------

def test_storefront_home_renders_successfully(client):
    """Regression test for a real bug found during manual testing: the installed
    starlette version uses TemplateResponse(request, name, context) -- passing the
    old-style TemplateResponse(name, {"request": request}) silently shifted arguments,
    causing a dict to be used where a template name string was expected
    (TypeError: unhashable type: 'dict'). This test locks in that '/' renders cleanly."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Products" in response.text


def test_static_css_is_served(client):
    response = client.get("/static/css/style.css")
    assert response.status_code == 200


def test_static_js_is_served(client):
    response = client.get("/static/js/storefront.js")
    assert response.status_code == 200

# ---------- Customer auth pages (Step 6a) ----------

def test_customer_login_page_renders(client):
    response = client.get("/account/login")
    assert response.status_code == 200
    assert "login-form" in response.text


def test_customer_signup_page_renders(client):
    response = client.get("/account/signup")
    assert response.status_code == 200
    assert "signup-form" in response.text