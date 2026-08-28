"""
Tests for merchant signup/login/logout and product ownership isolation
between different merchant accounts.
"""

import os
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("ANTHROPIC_API_KEY", "test_placeholder")
os.environ.setdefault("RAZORPAY_KEY_ID", "test_key")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "test_secret")
os.environ.setdefault("SESSION_SECRET_KEY", "test-secret-key")

from app.db_models import Base
from app import db as db_module
from app import main


@pytest.fixture(autouse=True)
def isolated_test_db(tmp_path):
    test_db_path = tmp_path / "test_merchant_auth.db"
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
    return TestClient(main.app)


def test_signup_creates_account_and_logs_in(client):
    response = client.post("/api/merchant/signup", json={
        "email": "owner@shop.com", "password": "securepass123", "store_name": "My Shop",
    })
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "owner@shop.com"
    assert body["store_name"] == "My Shop"
    assert "password" not in body and "password_hash" not in body


def test_signup_rejects_duplicate_email(client):
    client.post("/api/merchant/signup", json={
        "email": "dup@shop.com", "password": "securepass123", "store_name": "Shop A",
    })
    second = client.post("/api/merchant/signup", json={
        "email": "dup@shop.com", "password": "differentpass", "store_name": "Shop B",
    })
    assert second.status_code == 409


def test_signup_rejects_short_password(client):
    response = client.post("/api/merchant/signup", json={
        "email": "weak@shop.com", "password": "short", "store_name": "Shop",
    })
    assert response.status_code == 422


def test_login_with_correct_credentials(client):
    client.post("/api/merchant/signup", json={
        "email": "login@shop.com", "password": "correctpass123", "store_name": "Shop",
    })
    client.post("/api/merchant/logout")

    response = client.post("/api/merchant/login", json={
        "email": "login@shop.com", "password": "correctpass123",
    })
    assert response.status_code == 200


def test_login_with_wrong_password_rejected(client):
    client.post("/api/merchant/signup", json={
        "email": "wrongpass@shop.com", "password": "correctpass123", "store_name": "Shop",
    })
    response = client.post("/api/merchant/login", json={
        "email": "wrongpass@shop.com", "password": "incorrectpass",
    })
    assert response.status_code == 401


def test_login_with_nonexistent_email_rejected(client):
    response = client.post("/api/merchant/login", json={
        "email": "doesnotexist@shop.com", "password": "anypassword",
    })
    assert response.status_code == 401


def test_me_endpoint_requires_login(client):
    response = client.get("/api/merchant/me")
    assert response.status_code == 401


def test_me_endpoint_returns_profile_when_logged_in(client):
    client.post("/api/merchant/signup", json={
        "email": "me@shop.com", "password": "securepass123", "store_name": "Me Shop",
    })
    response = client.get("/api/merchant/me")
    assert response.status_code == 200
    assert response.json()["email"] == "me@shop.com"


def test_logout_clears_session(client):
    client.post("/api/merchant/signup", json={
        "email": "logout@shop.com", "password": "securepass123", "store_name": "Shop",
    })
    client.post("/api/merchant/logout")
    response = client.get("/api/merchant/me")
    assert response.status_code == 401


def test_create_product_requires_merchant_login(client):
    response = client.post("/api/products", json={"name": "Item", "price": 100, "stock": 5})
    assert response.status_code == 401


# ---------- Cross-merchant isolation (the actual multi-tenant security guarantee) ----------

def test_merchant_cannot_edit_another_merchants_product(client):
    """CRITICAL: Merchant A must not be able to modify Merchant B's product,
    even by guessing/iterating product IDs."""
    client.post("/api/merchant/signup", json={
        "email": "merchant_a@shop.com", "password": "passwordA123", "store_name": "Shop A",
    })
    product = client.post("/api/products", json={"name": "A's Item", "price": 500, "stock": 10}).json()
    client.post("/api/merchant/logout")

    client.post("/api/merchant/signup", json={
        "email": "merchant_b@shop.com", "password": "passwordB123", "store_name": "Shop B",
    })
    attempt = client.patch(f"/api/products/{product['id']}", json={"price": 1})
    assert attempt.status_code == 404  # not 403 -- never confirm the product exists to a non-owner


def test_merchant_cannot_delete_another_merchants_product(client):
    client.post("/api/merchant/signup", json={
        "email": "owner1@shop.com", "password": "passwordA123", "store_name": "Shop 1",
    })
    product = client.post("/api/products", json={"name": "Owner1 Item", "price": 500, "stock": 10}).json()
    client.post("/api/merchant/logout")

    client.post("/api/merchant/signup", json={
        "email": "owner2@shop.com", "password": "passwordB123", "store_name": "Shop 2",
    })
    attempt = client.delete(f"/api/products/{product['id']}")
    assert attempt.status_code == 404


def test_merchant_products_endpoint_only_shows_own_products(client):
    client.post("/api/merchant/signup", json={
        "email": "ownerX@shop.com", "password": "passwordX123", "store_name": "Shop X",
    })
    client.post("/api/products", json={"name": "X Item", "price": 100, "stock": 5})
    client.post("/api/merchant/logout")

    client.post("/api/merchant/signup", json={
        "email": "ownerY@shop.com", "password": "passwordY123", "store_name": "Shop Y",
    })
    client.post("/api/products", json={"name": "Y Item", "price": 200, "stock": 3})

    my_products = client.get("/api/merchant/products").json()
    names = [p["name"] for p in my_products]
    assert "Y Item" in names
    assert "X Item" not in names  # Shop Y must never see Shop X's own-products listing


def test_public_product_listing_shows_products_from_all_merchants(client):
    """The PUBLIC storefront listing is intentionally different from the merchant's
    own-products view: customers should see everyone's active products."""
    client.post("/api/merchant/signup", json={
        "email": "sellerA@shop.com", "password": "passwordA123", "store_name": "Seller A",
    })
    client.post("/api/products", json={"name": "Product From A", "price": 100, "stock": 5})
    client.post("/api/merchant/logout")

    client.post("/api/merchant/signup", json={
        "email": "sellerB@shop.com", "password": "passwordB123", "store_name": "Seller B",
    })
    client.post("/api/products", json={"name": "Product From B", "price": 200, "stock": 5})

    public_listing = client.get("/api/products").json()
    names = [p["name"] for p in public_listing]
    assert "Product From A" in names
    assert "Product From B" in names


# ---------- Merchant overview + products pages (Step 6c, split into sidebar sections) ----------

def test_overview_redirects_when_not_logged_in(client):
    response = client.get("/merchant", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/merchant/login"


def test_overview_renders_when_logged_in(client):
    client.post("/api/merchant/signup", json={
        "email": "dashboard@shop.com", "password": "dashpass123", "store_name": "My Cool Shop",
    })
    response = client.get("/merchant")
    assert response.status_code == 200
    assert "My Cool Shop" in response.text
    assert "Recovery Analytics" in response.text


def test_overview_redirects_again_after_logout(client):
    client.post("/api/merchant/signup", json={
        "email": "logout_dash@shop.com", "password": "dashpass123", "store_name": "Shop",
    })
    client.post("/api/merchant/logout")
    response = client.get("/merchant", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/merchant/login"


def test_products_page_redirects_when_not_logged_in(client):
    response = client.get("/merchant/products", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/merchant/login"


def test_products_page_renders_when_logged_in(client):
    client.post("/api/merchant/signup", json={
        "email": "products_page@shop.com", "password": "dashpass123", "store_name": "Shop",
    })
    response = client.get("/merchant/products")
    assert response.status_code == 200
    assert "add-product-btn" in response.text


def test_sidebar_links_present_and_correctly_marked_active(client):
    client.post("/api/merchant/signup", json={
        "email": "sidebar@shop.com", "password": "dashpass123", "store_name": "Shop",
    })
    overview = client.get("/merchant")
    assert 'href="/merchant/products"' in overview.text
    assert 'href="/merchant/orders"' in overview.text

    products = client.get("/merchant/products")
    assert 'class="sidebar-link active"' in products.text or 'sidebar-link active' in products.text


# ---------- Analytics endpoint (Step 6c-ii) ----------

def test_analytics_requires_merchant_login(client):
    response = client.get("/api/merchant/analytics")
    assert response.status_code == 401


def test_analytics_endpoint_returns_expected_shape(client):
    client.post("/api/merchant/signup", json={
        "email": "analytics_shape@shop.com", "password": "analyticspass123", "store_name": "Shop",
    })
    response = client.get("/api/merchant/analytics")
    assert response.status_code == 200
    body = response.json()
    for key in ("started_count", "completed_count", "abandoned_count",
                "total_abandoned_value", "total_amount_offered",
                "total_confirmed_recovered", "recovery_rate_pct",
                "action_breakdown", "classification_method_breakdown", "recent_outcomes"):
        assert key in body


def test_overview_page_includes_analytics_section(client):
    client.post("/api/merchant/signup", json={
        "email": "analytics_dash@shop.com", "password": "analyticspass123", "store_name": "Shop",
    })
    response = client.get("/merchant")
    assert "Recovery Analytics" in response.text
    assert "analytics-cards" in response.text