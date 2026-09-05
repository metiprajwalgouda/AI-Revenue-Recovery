/*
Merchant dashboard: loads the merchant's products, and handles
add/edit/deactivate/reactivate through the product API with live KPI cards and search.
*/

let merchantProducts = [];
let currentMerchantFilter = 'all';

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

function showDashboardError(message) {
    const banner = document.getElementById("error-banner");
    if (banner) {
        banner.textContent = message;
        banner.style.display = "block";
        banner.style.background = "#fef2f2";
        banner.style.color = "#b91c1c";
        banner.style.padding = "12px 16px";
        banner.style.borderRadius = "8px";
        banner.style.border = "1px solid #fecaca";
        banner.style.fontSize = "13px";
        banner.style.fontWeight = "600";
        setTimeout(() => { banner.style.display = "none"; }, 4500);
    } else {
        alert(message);
    }
}

async function loadMyProducts() {
    const tbody = document.getElementById("product-table-body");
    if (!tbody) return;
    tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: #6b7280; padding: 32px;">Loading catalog...</td></tr>`;

    try {
        const response = await fetch("/api/merchant/products");
        if (response.status === 401) {
            window.location.href = "/merchant/login";
            return;
        }
        if (!response.ok) throw new Error(`Server returned ${response.status}`);

        const data = await response.json();
        merchantProducts = Array.isArray(data) ? data : [];
        updateProductKPIs(merchantProducts);
        renderMerchantProductsTable();

        const searchInput = document.getElementById("merchant-prod-search");
        if (searchInput) {
            searchInput.oninput = renderMerchantProductsTable;
        }

    } catch (err) {
        console.error("Failed to load products:", err);
        if (tbody) {
            tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: #dc2626; padding: 24px;">Couldn't load products. Please refresh.</td></tr>`;
        }
    }
}

function updateProductKPIs(products) {
    const list = Array.isArray(products) ? products : [];
    const total = list.length;
    const active = list.filter(p => Boolean(p.is_active)).length;
    const lowStock = list.filter(p => Boolean(p.is_active) && (Number(p.stock) <= 5)).length;
    const totalUnits = list.reduce((acc, p) => acc + (Number(p.stock) || 0), 0);

    const totalEl = document.getElementById("kpi-total-prods");
    const activeEl = document.getElementById("kpi-active-prods");
    const lowStockEl = document.getElementById("kpi-low-stock");
    const unitsEl = document.getElementById("kpi-total-units");

    if (totalEl) totalEl.textContent = total;
    if (activeEl) activeEl.textContent = active;
    if (lowStockEl) lowStockEl.textContent = lowStock;
    if (unitsEl) unitsEl.textContent = totalUnits;
}

function setMerchantFilter(filter, el) {
    currentMerchantFilter = filter;
    document.querySelectorAll(".filter-pill-btn").forEach(b => b.classList.remove("active"));
    if (el) {
        el.classList.add("active");
    } else {
        const matchingBtn = Array.from(document.querySelectorAll(".filter-pill-btn")).find(b => 
            b.getAttribute("onclick") && b.getAttribute("onclick").includes(`'${filter}'`)
        );
        if (matchingBtn) matchingBtn.classList.add("active");
    }
    renderMerchantProductsTable();
}

function renderMerchantProductsTable() {
    const tbody = document.getElementById("product-table-body");
    if (!tbody) return;

    const searchInput = document.getElementById("merchant-prod-search");
    const term = searchInput ? searchInput.value.toLowerCase().trim() : "";

    let filtered = merchantProducts.filter(p => {
        if (term) {
            const matchName = (p.name || "").toLowerCase().includes(term);
            const matchDesc = (p.description || "").toLowerCase().includes(term);
            if (!matchName && !matchDesc) return false;
        }

        if (currentMerchantFilter === 'active') {
            return Boolean(p.is_active);
        }
        if (currentMerchantFilter === 'inactive') {
            return !p.is_active;
        }
        if (currentMerchantFilter === 'lowstock') {
            return Boolean(p.is_active) && (Number(p.stock) <= 5);
        }

        return true;
    });

    if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; padding: 36px; color: #6b7280;">No products match your search or filter criteria.</td></tr>`;
        return;
    }

    tbody.innerHTML = filtered.map(renderProductRow).join("");
}

function renderProductRow(product) {
    const imgSrc = product.image_url || "https://placehold.co/100x100/f3f4f6/1f2937?text=" + encodeURIComponent(product.name || 'Product');
    const stockVal = Number(product.stock) || 0;
    const lowStock = product.is_active && stockVal > 0 && stockVal <= 5;
    const outOfStock = stockVal === 0;

    let stockClass = "";
    let stockText = `${stockVal} units`;
    if (outOfStock) {
        stockClass = "out";
        stockText = "0 (Out of stock)";
    } else if (lowStock) {
        stockClass = "low";
        stockText = `${stockVal} (Low stock)`;
    }

    return `
        <tr data-product-id="${product.id}">
            <td>
                <div class="product-row-info">
                    <img src="${escapeHtml(imgSrc)}" alt="${escapeHtml(product.name)}" class="table-prod-img" onerror="this.onerror=null; this.src='https://placehold.co/100x100/f3f4f6/1f2937?text=Product';">
                    <div>
                        <div class="prod-name-title">${escapeHtml(product.name)}</div>
                        <div class="prod-desc-preview">${escapeHtml(product.description || "No description provided.")}</div>
                    </div>
                </div>
            </td>
            <td style="font-weight: 700; color: #111827;">₹${Number(product.price).toFixed(2)}</td>
            <td>
                <span class="stock-tag ${stockClass}">${stockText}</span>
            </td>
            <td>
                <span class="status-badge ${product.is_active ? 'status-active' : 'status-inactive'}">
                    ● ${product.is_active ? "Active" : "Inactive"}
                </span>
            </td>
            <td style="text-align: right;">
                <div class="action-btn-group" style="justify-content: flex-end;">
                    <a href="/product/${product.id}" target="_blank" class="btn-table-edit" style="text-decoration: none;">View ↗</a>
                    <button type="button" class="btn-table-edit" onclick="editProductById(${product.id})">Edit</button>
                    ${product.is_active 
                        ? `<button type="button" class="btn-table-deact" onclick="deactivateProductById(${product.id})">Deactivate</button>` 
                        : `<button type="button" class="btn-table-edit" style="background: #ecfdf5; color: #047857; border-color: #a7f3d0;" onclick="reactivateProductById(${product.id})">Reactivate</button>`
                    }
                </div>
            </td>
        </tr>
    `;
}

function openProductForm(product = null) {
    const backdrop = document.getElementById("product-modal-backdrop");
    const title = document.getElementById("product-form-title");
    
    const idInput = document.getElementById("prod-input-id");
    const nameInput = document.getElementById("prod-input-name");
    const descInput = document.getElementById("prod-input-desc");
    const priceInput = document.getElementById("prod-input-price");
    const stockInput = document.getElementById("prod-input-stock");
    const imgInput = document.getElementById("prod-input-img");

    if (product) {
        if (title) title.textContent = "Edit Product Details";
        if (idInput) idInput.value = product.id;
        if (nameInput) nameInput.value = product.name || "";
        if (descInput) descInput.value = product.description || "";
        if (priceInput) priceInput.value = product.price;
        if (stockInput) stockInput.value = product.stock;
        if (imgInput) imgInput.value = product.image_url || "";
    } else {
        if (title) title.textContent = "Add New Product";
        if (idInput) idInput.value = "";
        if (nameInput) nameInput.value = "";
        if (descInput) descInput.value = "";
        if (priceInput) priceInput.value = "";
        if (stockInput) stockInput.value = "";
        if (imgInput) imgInput.value = "";
    }
    
    if (backdrop) {
        backdrop.style.display = "flex";
    }
}

function closeProductForm() {
    const backdrop = document.getElementById("product-modal-backdrop");
    if (backdrop) {
        backdrop.style.display = "none";
    }
}

function editProductById(id) {
    const product = merchantProducts.find(p => Number(p.id) === Number(id));
    if (product) {
        openProductForm(product);
    }
}

async function deactivateProductById(id) {
    if (!confirm("Deactivate this product? It will no longer be visible to customers on the storefront.")) return;
    try {
        const response = await fetch(`/api/products/${id}`, { method: "DELETE" });
        if (response.ok) {
            await loadMyProducts();
        } else {
            const err = await response.json().catch(() => ({}));
            showDashboardError(err.detail || "Couldn't deactivate this product.");
        }
    } catch(e) {
        showDashboardError("Network error while deactivating product.");
    }
}

async function reactivateProductById(id) {
    try {
        const response = await fetch(`/api/products/${id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ is_active: true })
        });
        if (response.ok) {
            await loadMyProducts();
        } else {
            const err = await response.json().catch(() => ({}));
            showDashboardError(err.detail || "Couldn't reactivate this product.");
        }
    } catch(e) {
        showDashboardError("Network error while reactivating product.");
    }
}

async function handleProductFormSubmit(event) {
    if (event) event.preventDefault();
    
    const idInput = document.getElementById("prod-input-id");
    const nameInput = document.getElementById("prod-input-name");
    const descInput = document.getElementById("prod-input-desc");
    const priceInput = document.getElementById("prod-input-price");
    const stockInput = document.getElementById("prod-input-stock");
    const imgInput = document.getElementById("prod-input-img");
    const submitBtn = document.getElementById("btn-submit-product");

    const productId = idInput ? idInput.value.trim() : "";
    const name = nameInput ? nameInput.value.trim() : "";
    const description = descInput ? descInput.value.trim() : "";
    const price = parseFloat(priceInput ? priceInput.value : "0");
    const stock = parseInt(stockInput ? stockInput.value : "0", 10);
    const imageUrl = imgInput ? imgInput.value.trim() : "";

    if (!name) {
        showDashboardError("Please enter a product name.");
        return;
    }
    if (isNaN(price) || price <= 0) {
        showDashboardError("Please enter a valid price (greater than ₹0).");
        return;
    }
    if (isNaN(stock) || stock < 0) {
        showDashboardError("Please enter a valid stock quantity (0 or higher).");
        return;
    }

    const payload = {
        name: name,
        description: description || null,
        price: price,
        stock: stock,
        image_url: imageUrl || null,
    };

    const isEdit = Boolean(productId && productId !== "");
    const url = isEdit ? `/api/products/${productId}` : "/api/products";
    const method = isEdit ? "PATCH" : "POST";

    if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = "Saving...";
    }

    try {
        const response = await fetch(url, {
            method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });

        if (!response.ok) {
            const errorBody = await response.json().catch(() => ({}));
            showDashboardError(errorBody.detail || "Couldn't save the product.");
            return;
        }

        closeProductForm();
        await loadMyProducts();

    } catch (err) {
        console.error("Product save failed:", err);
        showDashboardError("Couldn't reach the server. Please try again.");
    } finally {
        if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = "Save Product";
        }
    }
}

// Bind functions to window immediately
window.openProductForm = openProductForm;
window.closeProductForm = closeProductForm;
window.editProductById = editProductById;
window.deactivateProductById = deactivateProductById;
window.reactivateProductById = reactivateProductById;
window.setMerchantFilter = setMerchantFilter;
window.handleProductFormSubmit = handleProductFormSubmit;
window.loadMyProducts = loadMyProducts;

// Initialize when ready
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
        loadMyProducts();
        window.addEventListener("click", (e) => {
            const backdrop = document.getElementById("product-modal-backdrop");
            if (e.target === backdrop) closeProductForm();
        });
    });
} else {
    loadMyProducts();
    window.addEventListener("click", (e) => {
        const backdrop = document.getElementById("product-modal-backdrop");
        if (e.target === backdrop) closeProductForm();
    });
}