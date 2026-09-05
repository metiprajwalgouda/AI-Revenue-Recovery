/*
Loads and renders the interactive product grid on the storefront home page.
Supports live search, category chip filtering, price/name sorting, and instant cart additions.
*/

let allProducts = [];
let currentFilter = 'all';
let currentSort = 'featured';

async function loadProducts() {
    const grid = document.getElementById("product-grid");
    try {
        const response = await fetch("/api/products");
        if (!response.ok) throw new Error(`Server returned ${response.status}`);
        allProducts = await response.json();

        if (allProducts.length === 0) {
            grid.innerHTML = `
                <div style="grid-column: 1/-1; text-align: center; padding: 48px; background: #fff; border-radius: 12px; border: 1px dashed #d1d5db;">
                    <div style="font-size: 32px; margin-bottom: 10px;">📦</div>
                    <h3 style="margin: 0 0 6px 0; color: #111827;">No products available</h3>
                    <p style="color: #6b7280; font-size: 14px;">Products added by merchants will appear here.</p>
                </div>
            `;
            return;
        }

        renderFilteredProducts();

        const searchInput = document.getElementById("product-search");
        if (searchInput) {
            searchInput.addEventListener("input", () => {
                renderFilteredProducts();
            });
        }
    } catch (err) {
        console.error("Failed to load products:", err);
        grid.innerHTML = `<div class="error-banner" style="grid-column: 1/-1;">Couldn't load products. Please refresh the page.</div>`;
    }
}

function setProductFilter(filter, el) {
    currentFilter = filter;
    document.querySelectorAll(".filter-chip").forEach(c => c.classList.remove("active"));
    if (el) el.classList.add("active");
    renderFilteredProducts();
}

function applyProductSort(sort) {
    currentSort = sort;
    renderFilteredProducts();
}

function renderFilteredProducts() {
    const grid = document.getElementById("product-grid");
    const searchInput = document.getElementById("product-search");
    const term = searchInput ? searchInput.value.toLowerCase().trim() : "";

    let filtered = allProducts.filter(p => {
        if (term) {
            const matchName = (p.name || "").toLowerCase().includes(term);
            const matchDesc = (p.description || "").toLowerCase().includes(term);
            if (!matchName && !matchDesc) return false;
        }

        if (currentFilter === 'under1000' && p.price >= 1000) return false;
        if (currentFilter === 'premium' && p.price < 2000) return false;
        if (currentFilter === 'instock' && (!p.stock || p.stock <= 0)) return false;

        return true;
    });

    if (currentSort === 'price-asc') {
        filtered.sort((a, b) => a.price - b.price);
    } else if (currentSort === 'price-desc') {
        filtered.sort((a, b) => b.price - a.price);
    } else if (currentSort === 'name') {
        filtered.sort((a, b) => a.name.localeCompare(b.name));
    }

    if (filtered.length === 0) {
        grid.innerHTML = `
            <div style="grid-column: 1/-1; text-align: center; padding: 48px; background: #fff; border-radius: 12px; border: 1px dashed #d1d5db;">
                <div style="font-size: 32px; margin-bottom: 10px;">🔍</div>
                <h3 style="margin: 0 0 6px 0; color: #111827;">No matching products</h3>
                <p style="color: #6b7280; font-size: 14px;">Try searching for a different keyword or clearing filters.</p>
            </div>
        `;
        return;
    }

    grid.innerHTML = filtered.map(renderProductCard).join("");

    // Wire up Add to cart buttons
    grid.querySelectorAll("[data-add-to-cart]").forEach(button => {
        button.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            const prodId = parseInt(button.dataset.productId, 10);
            const product = allProducts.find(p => p.id === prodId);
            if (product) {
                addToCart(product);
            }
            
            button.disabled = true;
            button.textContent = "Added ✓";
            button.style.background = "#15803d";
            
            setTimeout(() => {
                button.disabled = false;
                button.textContent = "Add to Cart";
                button.style.background = "#111827";
            }, 1200);
        });
    });
}

function renderProductCard(product) {
    const imgSrc = product.image_url || "https://placehold.co/400x300/f3f4f6/1f2937?text=" + encodeURIComponent(product.name);
    const lowStock = product.stock > 0 && product.stock <= 3;
    const outOfStock = product.stock === 0;

    let badgeHtml = "";
    if (outOfStock) {
        badgeHtml = `<span class="stock-badge badge-out-stock">Out of Stock</span>`;
    } else if (lowStock) {
        badgeHtml = `<span class="stock-badge badge-low-stock">Only ${product.stock} left</span>`;
    } else {
        badgeHtml = `<span class="stock-badge badge-in-stock">In Stock</span>`;
    }

    // Deterministic rating calculation for demo flair
    const rating = (4.5 + ((product.id * 7) % 5) / 10).toFixed(1);
    const reviewsCount = 12 + ((product.id * 19) % 80);

    return `
        <div class="product-card">
            <a href="/product/${product.id}" style="text-decoration: none; color: inherit; display: flex; flex-direction: column; flex-grow: 1;">
                <div class="product-img-wrap">
                    <img src="${escapeHtml(imgSrc)}" alt="${escapeHtml(product.name)}" onerror="this.onerror=null; this.src='https://placehold.co/400x300/f3f4f6/1f2937?text=Product';">
                    ${badgeHtml}
                </div>
                <div class="rating-row">
                    <span>★</span>
                    <span>${rating}</span>
                    <span style="color: #9ca3af; font-size: 11px;">(${reviewsCount})</span>
                </div>
                <h3 class="product-title">${escapeHtml(product.name)}</h3>
                <p class="product-desc">${escapeHtml(product.description || "Premium quality essentials engineered for modern performance.")}</p>
            </a>
            <div class="product-card-footer">
                <div class="product-price-val">₹${product.price.toFixed(2)}</div>
                <button
                    class="add-cart-btn"
                    data-add-to-cart
                    data-product-id="${product.id}"
                    ${outOfStock ? "disabled" : ""}
                >${outOfStock ? "Out of stock" : "Add to Cart"}</button>
            </div>
        </div>
    `;
}

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

async function checkCustomerGreeting() {
    try {
        const titleEl = document.getElementById("customer-greeting-title");
        if (!titleEl) return;
        const res = await fetch("/api/customer/me");
        if (res.ok) {
            const customer = await res.json();
            if (customer && customer.name) {
                titleEl.textContent = `Hello, ${customer.name}! 👋`;
                const subEl = document.getElementById("customer-greeting-subtitle");
                if (subEl) subEl.textContent = "Welcome back! Explore our curated products and special offers below.";
            }
        }
    } catch(e) {}
}

document.addEventListener("DOMContentLoaded", () => {
    loadProducts();
    checkCustomerGreeting();
});